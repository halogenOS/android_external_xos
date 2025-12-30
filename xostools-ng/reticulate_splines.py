#!/usr/bin/env python3
"""
Reticulate our splines - Create branches from upstream sources.

This script processes repositories with either merge-aosp or upstream attributes
and creates new branches from their upstream sources. It's essentially a branch
creation tool that fetches from upstream and pushes new branches to XOS remotes.
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
    handle_lfs_cleanup,
    safe_add_or_update_remote,
    truncate_project_name,
    create_xos
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
class SplineTask:
    """A single spline reticulation task."""
    project: ProjectInfo
    project_path: Path
    upstream_url: str
    upstream_rev: str
    target_branch: str
    is_aosp: bool = False
    is_tag: bool = False
    force_push: bool = False

@dataclass
class SplineResult:
    """Result of a spline reticulation operation."""
    project_path: str
    success: bool
    message: str
    was_skipped: bool = False
    created_repo: bool = False
    had_lfs: bool = False

# LFS cleanup function is now imported from xos_common

def perform_single_reticulation(task: SplineTask, dry_run: bool, use_create_xos: bool) -> SplineResult:
    """Perform spline reticulation for a single project."""
    project_path = task.project_path
    project_name = task.project.path

    try:
        if dry_run:
            # In dry-run mode, analyze what would be done without making changes
            notes = []

            # Check if directory exists
            if not project_path.exists():
                notes.append("would create directory")

            # Check if git repo exists
            if not (project_path / ".git").exists():
                notes.append("would initialize git repo")
            elif project_path.exists():
                # Try to analyze existing repo
                try:
                    repo = git.Repo(project_path)

                    # Check if XOS remote exists
                    if 'XOS' in [remote.name for remote in repo.remotes]:
                        xos_remote = repo.remote('XOS')
                        # Try to check if branch exists (read-only operation)
                        try:
                            if task.target_branch in [ref.name.split('/')[-1] for ref in xos_remote.refs]:
                                return SplineResult(
                                    project_name,
                                    True,
                                    f"ref {task.target_branch} already exists",
                                    was_skipped=True
                                )
                        except (git.exc.GitCommandError, AttributeError):
                            pass  # Can't check remote refs in dry-run
                    else:
                        notes.append("would add XOS remote")

                    # Check if upstream remote exists
                    if 'upstream' not in [remote.name for remote in repo.remotes]:
                        notes.append("would add upstream remote")

                    # Check if shallow
                    if GitOperations.is_shallow_repo(project_path):
                        notes.append("would unshallow repo")

                    # Check for LFS
                    lfsconfig_exists = (project_path / ".lfsconfig").exists()
                    gitattributes_has_lfs = False
                    gitattributes_path = project_path / ".gitattributes"
                    if gitattributes_path.exists():
                        with open(gitattributes_path, 'r') as f:
                            gitattributes_has_lfs = 'merge=lfs' in f.read()

                    if lfsconfig_exists or gitattributes_has_lfs:
                        notes.append("would handle LFS cleanup")

                except git.exc.InvalidGitRepositoryError:
                    notes.append("would initialize git repo")

            notes.append("would fetch upstream")
            notes.append("would checkout branch")
            if use_create_xos:
                notes.append("would create repo if needed")
            notes.append("would push to XOS")

            note_str = f" ({', '.join(notes)})" if notes else ""
            return SplineResult(
                project_name,
                True,
                f"would reticulate from {task.upstream_url}@{task.upstream_rev} -> {task.target_branch}{note_str}",
                was_skipped=False
            )

        # ===== ACTUAL EXECUTION MODE =====

        # Create directory if it doesn't exist
        project_path.mkdir(parents=True, exist_ok=True)

        # Initialize git repo if needed
        if not (project_path / ".git").exists():
            console.print(f"[cyan]{project_name}:[/cyan] Initializing git repository")
            git.Repo.init(project_path)

        repo = git.Repo(project_path)

        # Check for unstaged changes and untracked files before proceeding
        if repo.is_dirty(untracked_files=True):
            return SplineResult(project_name, False, "repository has unstaged changes or untracked files, cannot proceed")

        # Set up XOS remote
        xos_url = f"https://git.halogenos.org/halogenOS/{task.project.name}"
        xos_push_url = f"git@git.halogenos.org:halogenOS/{task.project.name}"

        xos_remote = safe_add_or_update_remote(repo, 'XOS', xos_url)
        # Set push URL using git command directly
        repo.git.remote('set-url', '--push', 'XOS', xos_push_url)

        # Check if target branch already exists on remote
        try:
            xos_remote.fetch()
            if task.target_branch in [ref.name.split('/')[-1] for ref in xos_remote.refs]:
                return SplineResult(
                    project_name,
                    True,
                    f"ref {task.target_branch} already exists",
                    was_skipped=True
                )
        except git.exc.GitCommandError:
            # Remote might not exist yet, continue
            pass

        # Set up upstream remote
        upstream_remote = safe_add_or_update_remote(repo, 'upstream', task.upstream_url)

        # Fetch from upstream
        console.print(f"[cyan]{project_name}:[/cyan] Fetching upstream")
        try:
            upstream_remote.fetch()
        except git.exc.GitCommandError as e:
            console.print(f"[yellow]{project_name}:[/yellow] Failed to fetch from upstream: {e}")
            return SplineResult(project_name, False, "upstream fetch failed", was_skipped=True)

        # Fetch from XOS (may fail if repo doesn't exist)
        try:
            console.print(f"[cyan]{project_name}:[/cyan] Fetching XOS")
            xos_remote.fetch()
        except git.exc.GitCommandError as e:
            console.print(f"[cyan]{project_name}:[/cyan] XOS repo doesn't exist yet (will be created)")

        # Check if shallow and unshallow if needed
        if GitOperations.is_shallow_repo(project_path):
            console.print(f"[cyan]{project_name}:[/cyan] Unshallowing repository")
            if not GitOperations.unshallow_repo(project_path):
                return SplineResult(project_name, False, "failed to unshallow repository")

        # Checkout the upstream revision to target branch
        console.print(f"[cyan]{project_name}:[/cyan] Checking out {task.upstream_rev} -> {task.target_branch}")

        if task.is_tag:
            # Direct checkout of tag
            repo.git.checkout(task.upstream_rev, B=task.target_branch)
        else:
            # Checkout upstream branch
            upstream_ref = f"upstream/{task.upstream_rev}"
            repo.git.checkout(upstream_ref, B=task.target_branch)

        # Create repository if needed
        created_repo = False
        if self.use_create_xos:
            console.print(f"[cyan]{project_name}:[/cyan] Creating repository (if it doesn't exist)")
            created_repo = create_xos(project_name)
            if not created_repo:
                console.print(f"[yellow]{project_name}:[/yellow] Skipping - repository creation failed (repo may already exist or tokens missing)")
                return SplineResult(project_name, False, "repository creation failed", was_skipped=True)

        # Check if shallow again after checkout
        if GitOperations.is_shallow_repo(project_path):
            console.print(f"[cyan]{project_name}:[/cyan] Unshallowing branch")
            if not GitOperations.unshallow_repo(project_path):
                return SplineResult(project_name, False, "failed to unshallow branch")

        # Handle LFS cleanup if needed
        had_lfs = False
        lfsconfig_exists = (project_path / ".lfsconfig").exists()
        gitattributes_has_lfs = False

        gitattributes_path = project_path / ".gitattributes"
        if gitattributes_path.exists():
            with open(gitattributes_path, 'r') as f:
                gitattributes_has_lfs = 'merge=lfs' in f.read()

        if lfsconfig_exists or gitattributes_has_lfs:
            had_lfs = True
            console.print(f"[cyan]{project_name}:[/cyan] Handling LFS cleanup")
            lfs_success, lfs_msg = handle_lfs_cleanup(project_path, dry_run=False)  # dry_run is False here since we're in execution mode
            if not lfs_success:
                # Cleanup repository on LFS failure
                try:
                    console.print(f"[yellow]{project_name}:[/yellow] Cleaning up repository after LFS failure")
                    repo.git.reset("--hard")
                except Exception:
                    pass  # Ignore cleanup errors
                return SplineResult(project_name, False, f"spline reticulation succeeded but LFS cleanup failed: {lfs_msg}")

        # Push to XOS
        console.print(f"[cyan]{project_name}:[/cyan] Pushing to XOS")
        push_flags = ["-f"] if task.force_push else []
        try:
            repo.git.push("XOS", f"HEAD:{task.target_branch}", *push_flags)
        except git.exc.GitCommandError as e:
            console.print(f"[yellow]{project_name}:[/yellow] Push failed - repository may not exist or you may not have permission: {e}")
            return SplineResult(project_name, False, "push failed", was_skipped=True)

        return SplineResult(
            project_name,
            True,
            "spline reticulated successfully",
            was_skipped=False,
            created_repo=created_repo,
            had_lfs=had_lfs
        )

    except Exception as e:
        # Cleanup repository on any failure
        try:
            if project_path.exists() and (project_path / ".git").exists():
                repo = git.Repo(project_path)
                console.print(f"[yellow]{project_name}:[/yellow] Cleaning up repository after failure")
                repo.git.reset("--hard")
        except Exception:
            pass  # Ignore cleanup errors
        return SplineResult(project_name, False, f"unexpected error: {str(e)}")

class SplineReticulator:
    def __init__(self, dry_run: bool = False, max_workers: int = 4, force_push: bool = False, single_path: Optional[str] = None):
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.force_push = force_push
        self.single_path = single_path
        self.top = get_android_top()

        # Check if create_xos can be used (tokens available)
        self.use_create_xos = True

    def get_xos_snippet_path(self) -> Path:
        """Get path to XOS.xml snippet."""
        snippet_path = self.top / "manifest/snippets/XOS.xml"
        if not snippet_path.exists():
            raise FileNotFoundError(f"XOS manifest snippet not found: {snippet_path}")
        return snippet_path

    def parse_spline_tasks(self) -> List[SplineTask]:
        """Parse projects that need spline reticulation."""
        tasks = []

        # Parse snippets and manifests
        snippet_path = self.get_xos_snippet_path()
        aosp_snippet_path = self.top / ".repo/manifests/default.xml"

        try:
            # Get ROM revision
            rom_revision = os.environ.get('ROM_REVISION') or os.environ.get('ROM_VERSION')

            # Parse XOS snippet to get projects and defaults
            snippet_tree = ET.parse(snippet_path)
            snippet_root = snippet_tree.getroot()

            # Get default remote and revision from XOS snippet
            default = snippet_root.find('default')
            default_remote = default.get('remote', 'XOS') if default is not None else 'XOS'
            default_revision = default.get('revision', f'refs/heads/{rom_revision}') if default is not None else f'refs/heads/{rom_revision}'

            # Build remotes map from XOS snippet
            remotes = {}
            for remote in snippet_root.findall('remote'):
                remote_name = remote.get('name')
                remote_revision = remote.get('revision', default_revision)
                remotes[remote_name] = remote_revision

            # Determine which projects to process
            if self.single_path:
                target_paths = [self.single_path]
            else:
                target_paths = []

                # Add projects with merge-aosp attribute from XOS snippet
                merge_aosp_paths = [p.get('path') for p in snippet_root.findall('project[@merge-aosp]')]
                target_paths.extend(merge_aosp_paths)

                # Add projects with upstream attribute from XOS snippet
                upstream_paths = [p.get('path') for p in snippet_root.findall('project[@upstream]')]
                target_paths.extend(upstream_paths)

            # Process each target path
            for path in target_paths:
                # Find project in XOS snippet
                project_elem = snippet_root.find(f"project[@path='{path}']")
                if project_elem is None:
                    console.print(f"[yellow]Warning: Project {path} not found in XOS snippet[/yellow]")
                    continue

                name = project_elem.get('name')
                remote = project_elem.get('remote', default_remote)
                revision = project_elem.get('revision')

                if not revision:
                    revision = remotes.get(remote, default_revision)

                short_revision = revision.replace('refs/heads/', '')

                # Check if this is an AOSP merge project
                aosp_project = snippet_root.find(f"project[@path='{path}'][@merge-aosp='true']")
                is_aosp = aosp_project is not None

                # Initialize is_tag for all projects
                is_tag = False

                if is_aosp:
                    # Handle AOSP project
                    console.print(f"[blue]Processing AOSP project: {path}[/blue]")

                    # Get AOSP path
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
                    upstream_rev = None
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

                        # Clean up revision (only remove refs/heads/ like the original script)
                        if upstream_rev:
                            upstream_rev = upstream_rev.replace('refs/heads/', '')

                    if not upstream_rev:
                        console.print(f"[red]Unable to determine AOSP upstream revision for {path}[/red]")
                        continue

                else:
                    # Handle regular upstream project
                    upstream_full = project_elem.get('upstream')
                    if not upstream_full:
                        console.print(f"[yellow]Warning: No upstream attribute for {path}[/yellow]")
                        continue

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
                        upstream_url = f"https://git.halogenos.org/halogenOS/{name}"

                project_info = ProjectInfo(path, name, remote)
                project_path = self.top / path

                # Skip if revision is empty and ROM_REVISION not set
                if not revision and not rom_revision:
                    console.print(f"[yellow]Warning: Unable to determine revision for {path}, skipping[/yellow]")
                    continue

                target_branch = short_revision if revision else rom_revision

                task = SplineTask(
                    project_info,
                    project_path,
                    upstream_url,
                    upstream_rev,
                    target_branch,
                    is_aosp=is_aosp,
                    is_tag=is_tag,
                    force_push=self.force_push
                )

                tasks.append(task)

        except ET.ParseError as e:
            console.print(f"[red]Failed to parse XOS snippet: {e}[/red]")
            raise
        except Exception as e:
            console.print(f"[red]Error parsing spline tasks: {e}[/red]")
            raise

        return tasks

    def display_spline_results(self, successful_results: List[SplineResult], failed_results: List[SplineResult]):
        """Display spline reticulation results in a formatted table."""
        if not successful_results and not failed_results:
            return

        # truncate_project_name function is now imported from xos_common

        # Display successful reticulations
        if successful_results:
            console.print(f"\n[bold green]Successful spline reticulations ({len(successful_results)}):[/bold green]")

            table = Table(show_header=True, header_style="bold blue")
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Status", style="green")
            table.add_column("Notes", style="dim")
            table.add_column("", width=2)  # Status emoji

            for result in successful_results:
                notes = []
                if result.was_skipped:
                    status = "Skipped"
                    notes.append("already exists")
                else:
                    status = "Created"
                    if result.created_repo:
                        notes.append("new repo")
                    if result.had_lfs:
                        notes.append("LFS cleanup")

                table.add_row(
                    truncate_project_name(result.project_path),
                    status,
                    ", ".join(notes) if notes else "",
                    "✅" if not result.was_skipped else "⏭️"
                )

            console.print(table)

        # Display failed reticulations
        if failed_results:
            console.print(f"\n[bold red]Failed spline reticulations ({len(failed_results)}):[/bold red]")

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

    def process_splines(self, tasks: List[SplineTask]) -> Tuple[List[SplineResult], List[SplineResult]]:
        """Process all spline reticulation tasks using multi-threading."""
        global executor

        if not tasks:
            console.print("[yellow]No projects found for spline reticulation[/yellow]")
            return [], []

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        console.print(f"\n[bold]{dry_run_prefix}Processing {len(tasks)} spline reticulations...[/bold]")

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

            spline_task = progress.add_task(
                f"[cyan]Reticulating splines {dry_run_prefix.strip()}",
                total=len(tasks)
            )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor_pool:
                executor = executor_pool

                # Submit all tasks
                futures = {
                    executor_pool.submit(perform_single_reticulation, task, self.dry_run, self.use_create_xos): task
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

                        progress.update(spline_task, advance=1)

                    except Exception as e:
                        task = futures[future]
                        failed_result = SplineResult(task.project.path, False, f"exception: {str(e)}")
                        failed_results.append(failed_result)
                        progress.update(spline_task, advance=1)

        return successful_results, failed_results

    def run(self):
        """Main execution flow."""
        try:
            console.print("[cyan]Parsing projects for spline reticulation...[/cyan]")
            tasks = self.parse_spline_tasks()

        except Exception as e:
            console.print(f"[red]Failed to prepare spline tasks: {e}[/red]")
            return 1

        # Process splines
        successful_results, failed_results = self.process_splines(tasks)

        # Display results in table format
        self.display_spline_results(successful_results, failed_results)

        # Summary
        console.print("\n" + "=" * 60)
        console.print("[bold]Spline reticulation complete![/bold]")
        console.print(f"[green]Successfully processed:[/green] {len(successful_results)}/{len(tasks)} projects")

        skipped_count = len([r for r in successful_results if r.was_skipped])
        if skipped_count > 0:
            console.print(f"[blue]Skipped (already exist):[/blue] {skipped_count} projects")

        if failed_results:
            console.print(f"[red]Failed:[/red] {len(failed_results)} projects")

        if failed_results:
            console.print("\n[bold red]Completed with errors.[/bold red]")
            return 1
        else:
            console.print("\n[bold green]All splines successfully reticulated.[/bold green]")
            return 0

def main():
    parser = argparse.ArgumentParser(
        description="Reticulate splines - Create branches from upstream sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Reticulate all splines
  %(prog)s --dry-run                 # Show what would be done without making changes
  %(prog)s --single external/avb     # Process only a specific project
  %(prog)s --force                   # Force push branches (overwrites existing)
  %(prog)s --workers 2               # Use 2 parallel workers
        """
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
        "--force",
        action="store_true",
        help="Force push branches (equivalent to FORCE_PUSHES=true)"
    )

    parser.add_argument(
        "--single",
        type=str,
        help="Process only a specific project path"
    )

    args = parser.parse_args()

    try:
        reticulator = SplineReticulator(
            dry_run=args.dry_run,
            max_workers=args.workers,
            force_push=args.force,
            single_path=args.single
        )

        return reticulator.run()

    except KeyboardInterrupt:
        console.print("\n[red]Aborted by user[/red]")
        return 130
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1

if __name__ == "__main__":
    sys.exit(main())