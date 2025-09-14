#!/usr/bin/env python3
"""
Merge branches across all repositories.

This script merges a source branch into a target branch across all repositories
in the Android source tree. It's useful after performing bulk changes or
security patch cherry-picks to different branches.
"""

import os
import sys
import signal
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.table import Table
from dataclasses import dataclass
import threading
import git

from xos_common import (
    ManifestParser,
    GitOperations,
    get_android_top,
    ProjectInfo,
    console,
    safe_add_or_update_remote,
    generate_manifest,
    cleanup_manifest,
    truncate_project_name,
    track_project_in_xos,
    find_project_in_default_manifest,
    create_branch_from_upstream,
    merge_branch_into_current,
    branch_exists_locally,
    setup_tracked_project_branch,
    is_project_tracked_in_xos,
    get_xos_target_branch_for_tracked_project,
    GITLAB_URL
)

# Global stop event for graceful shutdown
stop_event = threading.Event()
executor = None

def signal_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) gracefully."""
    console.print("\n[red]Interrupted! Shutting down workers...[/red]")
    stop_event.set()
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)
    sys.exit(1)

# Set up signal handler
signal.signal(signal.SIGINT, signal_handler)

@dataclass
class MergeTask:
    """A single branch merge task."""
    project: ProjectInfo
    project_path: Path
    source_branch: str
    target_branch: str
    upstream_url: Optional[str] = None
    upstream_rev: Optional[str] = None
    is_aosp: bool = False
    is_tag: bool = False

@dataclass
class MergeResult:
    """Result of a branch merge operation."""
    project_path: str
    success: bool
    message: str
    was_skipped: bool = False
    created_target_branch: bool = False
    had_conflicts: bool = False
    needs_tracking: bool = False

def perform_single_merge(task: MergeTask, dry_run: bool) -> MergeResult:
    """Perform branch merge for a single project."""
    project_path = task.project_path
    project_name = task.project.path

    try:
        # Check if repository exists
        if not project_path.exists() or not (project_path / ".git").exists():
            return MergeResult(
                project_name,
                True,
                "repository does not exist",
                was_skipped=True
            )

        if dry_run:
            # Dry run analysis
            notes = []

            repo = git.Repo(project_path)

            # Check if source branch exists
            if not branch_exists_locally(project_path, task.source_branch):
                return MergeResult(
                    project_name,
                    True,
                    f"source branch {task.source_branch} does not exist",
                    was_skipped=True
                )

            # Check if target branch exists
            if not branch_exists_locally(project_path, task.target_branch):
                if task.upstream_url and task.upstream_rev:
                    notes.append(f"would create {task.target_branch} from upstream")
                else:
                    # Check if this project exists in default manifest
                    in_default = find_project_in_default_manifest(task.project.path) is not None

                    if in_default:
                        is_tracked = is_project_tracked_in_xos(task.project.path)

                        if not is_tracked:
                            notes.append("would track project in XOS manifests")

                        # Set up the branch from AOSP using the task's target branch
                        success, message = setup_tracked_project_branch(project_path, task.project.path, task.target_branch, dry_run=True)
                        if success:
                            notes.append(message)
                        else:
                            return MergeResult(project_name, False, f"failed to set up tracked project branch: {message}")
                    else:
                        return MergeResult(
                            project_name,
                            False,
                            f"target branch {task.target_branch} does not exist and no upstream defined"
                        )
            else:
                notes.append(f"would checkout {task.target_branch}")

            notes.append(f"would merge {task.source_branch}")

            note_str = f" ({', '.join(notes)})" if notes else ""
            return MergeResult(
                project_name,
                True,
                f"would merge {task.source_branch} -> {task.target_branch}{note_str}",
                was_skipped=False
            )

        # ===== ACTUAL EXECUTION MODE =====

        repo = git.Repo(project_path)

        # Check if source branch exists locally
        if not branch_exists_locally(project_path, task.source_branch):
            return MergeResult(
                project_name,
                True,
                f"source branch {task.source_branch} does not exist",
                was_skipped=True
            )

        created_target_branch = False

        # Check if target branch exists, create if needed
        if not branch_exists_locally(project_path, task.target_branch):
            if not task.upstream_url or not task.upstream_rev:
                # Check if this project exists in default manifest
                in_default = find_project_in_default_manifest(task.project.path) is not None

                if in_default:
                    # Always try to track the project (function will skip if already tracked)
                    console.print(f"[yellow]{project_name}:[/yellow] Ensuring project is tracked in XOS manifests")
                    track_success = track_project_in_xos(task.project.path, dry_run=dry_run)
                    if track_success:
                        console.print(f"[blue]Note: merge-aosp=\"true\" is set. upstream=\"\" may need to be added if required.[/blue]")

                    # Set up the branch from upstream
                    success, message = setup_tracked_project_branch(project_path, task.project.path, task.target_branch, dry_run=dry_run)
                    if not success:
                        return MergeResult(project_name, False, f"failed to set up tracked project branch: {message}")

                    created_target_branch = True
                else:
                    return MergeResult(
                        project_name,
                        False,
                        f"target branch {task.target_branch} does not exist and no upstream defined"
                    )

            # Set up upstream remote if needed
            upstream_remote = safe_add_or_update_remote(repo, 'upstream', task.upstream_url)

            # Fetch from upstream
            console.print(f"[cyan]{project_name}:[/cyan] Fetching upstream")
            upstream_remote.fetch()

            # Create target branch from upstream
            console.print(f"[cyan]{project_name}:[/cyan] Creating {task.target_branch} from upstream")

            if task.is_tag:
                # Direct checkout of tag
                tag_name = task.upstream_rev.replace('refs/tags/', '') if task.upstream_rev.startswith('refs/tags/') else task.upstream_rev
                repo.git.checkout(tag_name, B=task.target_branch)
            else:
                # Checkout upstream branch
                branch_name = task.upstream_rev.replace('refs/heads/', '') if task.upstream_rev.startswith('refs/heads/') else task.upstream_rev
                upstream_ref = f"upstream/{branch_name}"
                repo.git.checkout(upstream_ref, B=task.target_branch)

            created_target_branch = True

        else:
            # Target branch exists, check it out
            console.print(f"[cyan]{project_name}:[/cyan] Checking out {task.target_branch}")
            repo.heads[task.target_branch].checkout()

        # Perform the merge
        console.print(f"[cyan]{project_name}:[/cyan] Merging {task.source_branch} into {task.target_branch}")
        success, message = merge_branch_into_current(project_path, task.source_branch, dry_run=False)

        had_conflicts = "merge conflicts" in message.lower()

        if not success and not had_conflicts:
            return MergeResult(
                project_name,
                False,
                f"merge failed: {message}",
                created_target_branch=created_target_branch
            )

        return MergeResult(
            project_name,
            True,
            message,
            was_skipped=False,
            created_target_branch=created_target_branch,
            had_conflicts=had_conflicts
        )

    except Exception as e:
        return MergeResult(project_name, False, f"unexpected error: {str(e)}")

class BranchMerger:
    def __init__(self, source_branch: str, target_branch: Optional[str] = None, dry_run: bool = False,
                 max_workers: int = 4, single_path: Optional[str] = None):
        self.source_branch = source_branch
        self.target_branch = target_branch
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.single_path = single_path
        self.top = get_android_top()

    def generate_manifest(self) -> Path:
        """Generate temporary manifest file."""
        return generate_manifest(self.top, self.dry_run)

    def parse_merge_tasks(self, manifest_path: Path) -> List[MergeTask]:
        """Parse all projects and create merge tasks."""
        tasks = []

        try:
            # Get ROM revision
            rom_revision = os.environ.get('ROM_REVISION') or os.environ.get('ROM_VERSION')

            manifest_tree = ET.parse(manifest_path)
            manifest_root = manifest_tree.getroot()

            # Get default remote and revision
            default = manifest_root.find('default')
            default_remote = default.get('remote', 'XOS') if default is not None else 'XOS'
            default_revision = default.get('revision', f'refs/heads/{rom_revision}') if default is not None else f'refs/heads/{rom_revision}'

            # Build remotes map
            remotes = {}
            for remote in manifest_root.findall('remote'):
                remote_name = remote.get('name')
                remote_revision = remote.get('revision', default_revision)
                remotes[remote_name] = remote_revision

            # Determine which projects to process
            if self.single_path:
                # Find specific project
                project_elem = manifest_root.find(f"project[@path='{self.single_path}']")
                if project_elem is None:
                    console.print(f"[red]Project {self.single_path} not found in manifest[/red]")
                    return []
                projects = [project_elem]
            else:
                # Process all projects
                projects = manifest_root.findall('project')

            # Process each project
            for project_elem in projects:
                path = project_elem.get('path')
                name = project_elem.get('name')

                # Skip removed projects (they don't have a path)
                if path is None:
                    continue


                remote = project_elem.get('remote', default_remote)
                revision = project_elem.get('revision')

                if not revision:
                    revision = remotes.get(remote, default_revision)

                short_revision = revision.replace('refs/heads/', '') if revision else None

                # Determine target branch for this project based on manifest hierarchy
                if self.target_branch is None:
                    # No target branch provided - determine from manifest

                    # First check if project exists in XOS.xml
                    top = get_android_top()
                    xos_snippet_path = top / ".repo/manifests/snippets/XOS.xml"
                    xos_project_revision = None
                    xos_remote_revision = None

                    if xos_snippet_path.exists():
                        xos_tree = ET.parse(xos_snippet_path)
                        xos_root = xos_tree.getroot()

                        # Check if project has specific revision in XOS.xml
                        xos_project = xos_root.find(f"project[@path='{path}']")
                        if xos_project is not None:
                            xos_project_revision = xos_project.get('revision')

                        # Get XOS remote default revision
                        xos_remote = xos_root.find("remote[@name='XOS']")
                        if xos_remote is not None:
                            xos_remote_revision = xos_remote.get('revision')

                    # Determine target branch based on hierarchy
                    if xos_project_revision:
                        # Project has specific revision in XOS.xml
                        project_target_branch = xos_project_revision.replace('refs/heads/', '') if xos_project_revision else None
                    elif xos_remote_revision:
                        # Use XOS remote default revision
                        project_target_branch = xos_remote_revision.replace('refs/heads/', '') if xos_remote_revision else None
                    else:
                        # Fallback to project's current revision from manifest
                        project_target_branch = short_revision
                else:
                    # Target branch was explicitly provided
                    project_target_branch = self.target_branch

                # Initialize upstream info
                upstream_url = None
                upstream_rev = None
                is_aosp = False
                is_tag = False

                # Check if this project has upstream or merge-aosp configuration
                # Look for project in XOS.xml snippet for merge-aosp
                snippet_path = self.top / ".repo/manifests/snippets/XOS.xml"
                if snippet_path.exists():
                    snippet_tree = ET.parse(snippet_path)
                    snippet_root = snippet_tree.getroot()
                    aosp_project = snippet_root.find(f"project[@path='{path}'][@merge-aosp='true']")
                    if aosp_project is not None:
                        is_aosp = True

                # Check for upstream attribute in main manifest
                upstream_full = project_elem.get('upstream')

                if is_aosp:
                    # Handle AOSP project - get upstream info from default.xml
                    aosp_snippet_path = self.top / ".repo/manifests/default.xml"
                    if aosp_snippet_path.exists():
                        aosp_tree = ET.parse(aosp_snippet_path)
                        aosp_root = aosp_tree.getroot()
                        aosp_project = aosp_root.find(f"project[@path='{path}']")
                        if aosp_project is not None:
                            aosp_name = aosp_project.get('name')
                        else:
                            aosp_name = f"platform/{path}"
                    else:
                        aosp_name = f"platform/{path}"

                    upstream_url = f"https://android.googlesource.com/{aosp_name}"

                    # Get AOSP revision
                    if aosp_snippet_path.exists():
                        aosp_tree = ET.parse(aosp_snippet_path)
                        aosp_root = aosp_tree.getroot()

                        # Try to get revision from aosp remote first
                        aosp_remote = aosp_root.find("remote[@name='aosp']")
                        if aosp_remote is not None:
                            upstream_rev = aosp_remote.get('revision')

                        # If not found, try default with remote='aosp'
                        if not upstream_rev:
                            default_aosp = aosp_root.find("default[@remote='aosp']")
                            if default_aosp is not None:
                                upstream_rev = default_aosp.get('revision')

                        # Clean up revision
                        if upstream_rev:
                            upstream_rev = upstream_rev.replace('refs/heads/', '')

                elif upstream_full:
                    # Handle regular upstream project
                    if '|' in upstream_full or '#' in upstream_full:
                        # External upstream with specific revision
                        parts = upstream_full.replace('#', '|').split('|')
                        upstream_url = parts[0]
                        upstream_rev = parts[1] if len(parts) > 1 else "main"

                        # Check for tag/commit specification
                        if len(parts) > 2 and parts[1] == "tag":
                            is_tag = True
                            upstream_rev = parts[2]
                    else:
                        # Internal halogenOS repository
                        upstream_rev = upstream_full
                        upstream_url = f"{GITLAB_URL}/halogenOS/{name}"

                # Also check if upstream_rev indicates this is a tag
                if upstream_rev and upstream_rev.startswith('refs/tags/'):
                    is_tag = True

                project_info = ProjectInfo(path, name, remote)
                project_path = self.top / path

                task = MergeTask(
                    project_info,
                    project_path,
                    self.source_branch,
                    project_target_branch,
                    upstream_url,
                    upstream_rev,
                    is_aosp=is_aosp,
                    is_tag=is_tag
                )

                tasks.append(task)

        except ET.ParseError as e:
            console.print(f"[red]Failed to parse manifest: {e}[/red]")
            raise
        except Exception as e:
            console.print(f"[red]Error parsing merge tasks: {e}[/red]")
            raise

        return tasks

    def display_merge_results(self, successful_results: List[MergeResult], failed_results: List[MergeResult]):
        """Display merge results in a formatted table."""
        if not successful_results and not failed_results:
            return

        # Display successful merges (excluding skipped ones)
        non_skipped_results = [r for r in successful_results if not r.was_skipped]
        if non_skipped_results:
            console.print(f"\n[bold green]Successful merges ({len(non_skipped_results)}):[/bold green]")

            table = Table(show_header=True, header_style="bold blue")
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Status", style="green")
            table.add_column("Notes", style="dim")
            table.add_column("", width=2)  # Status emoji

            for result in non_skipped_results:
                notes = []
                if result.had_conflicts:
                    status = "Conflicts"
                    notes.append("needs manual resolution")
                    emoji = "⚠️"
                else:
                    status = "Merged"
                    emoji = "✅"

                if result.created_target_branch:
                    notes.append("created target branch")

                # Show both notes and merge message
                message_parts = []
                if notes:
                    message_parts.extend(notes)
                if result.message:
                    message_parts.append(result.message)

                table.add_row(
                    truncate_project_name(result.project_path),
                    status,
                    ", ".join(message_parts),
                    emoji
                )

            console.print(table)

        # Display failed merges
        if failed_results:
            console.print(f"\n[bold red]Failed merges ({len(failed_results)}):[/bold red]")

            fail_table = Table(show_header=True, header_style="bold red")
            fail_table.add_column("Project", style="cyan", no_wrap=True)
            fail_table.add_column("Error", style="red")
            fail_table.add_column("", width=2)  # Status emoji

            for result in failed_results:
                fail_table.add_row(
                    truncate_project_name(result.project_path),
                    result.message,
                    "❌"
                )

            console.print(fail_table)

    def process_merges(self, tasks: List[MergeTask]) -> Tuple[List[MergeResult], List[MergeResult]]:
        """Process all merge tasks using multi-threading."""
        global executor

        if not tasks:
            console.print("[yellow]No projects found for merging[/yellow]")
            return [], []

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        console.print(f"\n[bold]{dry_run_prefix}Processing {len(tasks)} repository merges...[/bold]")

        successful_results = []
        failed_results = []

        # Process with progress bar and threading
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:

            merge_task = progress.add_task(
                f"[cyan]Merging branches {dry_run_prefix.strip()}",
                total=len(tasks)
            )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor_pool:
                executor = executor_pool

                # Submit all tasks
                futures = {
                    executor_pool.submit(perform_single_merge, task, self.dry_run): task
                    for task in tasks
                }

                # Process results as they complete
                for future in as_completed(futures):
                    if stop_event.is_set():
                        executor_pool.shutdown(wait=False, cancel_futures=True)
                        break

                    try:
                        task = futures[future]
                        result = future.result()

                        if result.success:
                            successful_results.append(result)
                        else:
                            failed_results.append(result)

                        progress.update(merge_task, advance=1)

                    except Exception as e:
                        task = futures[future]
                        failed_result = MergeResult(task.project.path, False, f"exception: {str(e)}")
                        failed_results.append(failed_result)
                        progress.update(merge_task, advance=1)

        return successful_results, failed_results

    def run(self):
        """Main execution flow."""
        try:
            console.print("[cyan]Generating temporary manifest file...[/cyan]")
            manifest_path = self.generate_manifest()

            console.print("[cyan]Parsing projects for branch merging...[/cyan]")
            tasks = self.parse_merge_tasks(manifest_path)

        except Exception as e:
            console.print(f"[red]Failed to prepare merge tasks: {e}[/red]")
            return 1

        # Process merges
        successful_results, failed_results = self.process_merges(tasks)

        # Display results in table format
        self.display_merge_results(successful_results, failed_results)

        # Clean up temporary manifest
        cleanup_manifest(manifest_path, self.dry_run)

        # Check for projects that need tracking
        needs_tracking = []
        for result in successful_results:
            if not result.was_skipped and result.success:
                # Check if project is tracked in XOS manifests
                project_path = result.project_path
                if find_project_in_default_manifest(project_path) is not None:
                    # Project exists in default.xml, check if tracked in XOS
                    from xos_common import is_project_tracked_in_xos
                    if not is_project_tracked_in_xos(project_path):
                        needs_tracking.append(project_path)

        # Track projects that need it
        if needs_tracking and not self.dry_run:
            console.print(f"\n[yellow]Tracking {len(needs_tracking)} projects in XOS manifests...[/yellow]")
            for project_path in needs_tracking:
                project_element = find_project_in_default_manifest(project_path)
                if project_element is not None:
                    track_project_in_xos(project_path, dry_run=False)
                    merge_aosp_note = ' (merge-aosp="true" was set)' if project_element is not None else ' (upstream="" may need to be added)'
                    console.print(f"  Tracked {project_path}{merge_aosp_note}")

        # Summary
        console.print("\n" + "=" * 60)
        console.print("[bold]Branch merge complete![/bold]")
        console.print(f"[green]Successfully processed:[/green] {len(successful_results)}/{len(tasks)} projects")

        skipped_count = len([r for r in successful_results if r.was_skipped])
        if skipped_count > 0:
            console.print(f"[blue]Skipped (no source branch):[/blue] {skipped_count} projects")

        conflicts_count = len([r for r in successful_results if r.had_conflicts])
        if conflicts_count > 0:
            console.print(f"[yellow]With conflicts:[/yellow] {conflicts_count} projects (need manual resolution)")

        if failed_results:
            console.print(f"[red]Failed:[/red] {len(failed_results)} projects")

        if failed_results or conflicts_count > 0:
            console.print("\n[bold yellow]Completed with issues that need attention.[/bold yellow]")
            return 1
        else:
            console.print("\n[bold green]All merges completed successfully.[/bold green]")
            return 0

def main():
    parser = argparse.ArgumentParser(
        description="Merge branches across all repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s feature-branch main           # Merge feature-branch into main
  %(prog)s feature-branch                # Merge feature-branch into each project's manifest branch
  %(prog)s --dry-run patch-branch dev    # Show what would be merged
  %(prog)s --dry-run patch-branch        # Show what would be merged into manifest branches
  %(prog)s --single external/avb fix stable  # Merge only in specific project
  %(prog)s --workers 2 hotfix release   # Use 2 parallel workers
        """
    )

    parser.add_argument(
        "source_branch",
        help="Name of the source branch to merge from (must exist locally)"
    )

    parser.add_argument(
        "target_branch",
        nargs="?",
        help="Name of the target branch to merge into (will be created if needed). If not specified, uses each project's current manifest revision"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making any changes"
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)"
    )

    parser.add_argument(
        "--single",
        type=str,
        help="Process only a specific project path"
    )

    args = parser.parse_args()

    try:
        merger = BranchMerger(
            source_branch=args.source_branch,
            target_branch=args.target_branch,
            dry_run=args.dry_run,
            max_workers=args.workers,
            single_path=args.single
        )

        return merger.run()

    except KeyboardInterrupt:
        console.print("\n[red]Aborted by user[/red]")
        return 130
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1

if __name__ == "__main__":
    sys.exit(main())