#!/usr/bin/env python3
"""
Merge upstream changes for repositories.
Version: 16.0

Merges upstream changes from remote repositories into local branches,
supporting multi-threading, progress bars, and deferred pushing.
"""

import os
import sys
import signal
import argparse
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.console import Console
from dataclasses import dataclass
import threading
import tempfile
import git

from xos_common import (
    ManifestParser,
    GitOperations,
    get_android_top,
    ProjectInfo
)

console = Console()

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
class UpstreamConfig:
    """Configuration for upstream merging."""
    upstream_url: str
    upstream_rev: str
    is_tag_or_commit: bool = False

@dataclass
class MergeTask:
    """A single merge task."""
    project: ProjectInfo
    project_path: Path
    upstream_config: UpstreamConfig
    repo_remote: str
    repo_revision: str
    short_revision: str

@dataclass
class MergeResult:
    """Result of a merge operation."""
    project_path: str
    success: bool
    message: str
    needs_push: bool = False
    push_command: Optional[str] = None
    had_lfs: bool = False


def parse_upstream_config(upstream_full: str, repo_name: str) -> UpstreamConfig:
    """Parse upstream configuration from manifest."""
    if '|' in upstream_full or '#' in upstream_full:
        # External upstream with specific revision
        parts = upstream_full.replace('#', '|').split('|')
        upstream_url = parts[0]
        upstream_rev = parts[1] if len(parts) > 1 else "main"

        # Check for tag/commit specification
        is_tag_or_commit = False
        if len(parts) > 2 and parts[1] in ["tag", "commit"]:
            is_tag_or_commit = True
            upstream_rev = parts[2]

        return UpstreamConfig(upstream_url, upstream_rev, is_tag_or_commit)
    else:
        # Internal halogenOS repository
        upstream_url = f"https://git.halogenos.org/halogenOS/{repo_name}"
        return UpstreamConfig(upstream_url, upstream_full, False)


def handle_lfs_cleanup(repo_path: Path, dry_run: bool = False) -> Tuple[bool, str]:
    """Handle Git LFS cleanup (equivalent to unLFS function)."""
    try:
        repo = git.Repo(repo_path)

        # Check if LFS is present
        lfsconfig_exists = (repo_path / ".lfsconfig").exists()
        gitattributes_has_lfs = False

        gitattributes_path = repo_path / ".gitattributes"
        if gitattributes_path.exists():
            with open(gitattributes_path, 'r') as f:
                gitattributes_has_lfs = 'merge=lfs' in f.read()

        if not (lfsconfig_exists or gitattributes_has_lfs):
            return True, "no LFS detected"

        if dry_run:
            return True, "would handle LFS cleanup (dry run)"

        # Equivalent of the shell script unLFS function
        commands = [
            ["git", "lfs", "install"],
            ["git", "lfs", "fetch"],
            ["git", "lfs", "checkout"]
        ]

        # Run LFS commands
        for cmd in commands:
            try:
                subprocess.run(cmd, cwd=repo_path, check=False, capture_output=True, text=True)
            except Exception:
                pass  # Ignore errors, just like the shell script with || :

        # Get LFS files
        result = subprocess.run(
            ["git", "lfs", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            lfs_files = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 3:
                    lfs_files.append(parts[2])

            if lfs_files:
                # Remove from cache and untrack
                for lfs_file in lfs_files:
                    subprocess.run(["git", "rm", "--cached", lfs_file], cwd=repo_path, check=False, capture_output=True)
                    subprocess.run(["git", "lfs", "untrack", lfs_file], cwd=repo_path, check=False, capture_output=True)

        # Remove LFS config files
        for config_file in [".gitattributes", ".lfsconfig"]:
            config_path = repo_path / config_file
            if config_path.exists():
                config_path.unlink()

        # Stage and commit removal
        repo.index.add([".gitattributes", ".lfsconfig"])
        try:
            repo.index.commit("Un-LFS")
        except Exception:
            pass  # Ignore if nothing to commit

        # Uninstall LFS
        subprocess.run(["git", "lfs", "uninstall"], cwd=repo_path, check=False, capture_output=True)

        # Add LFS files directly
        if lfs_files:
            for lfs_file in lfs_files:
                repo.index.add([lfs_file])

        repo.index.add_all()
        try:
            repo.index.commit("Directly checkout LFS files")
        except Exception:
            pass  # Ignore if nothing to commit

        return True, "LFS cleanup completed"

    except Exception as e:
        return False, f"LFS cleanup failed: {str(e)}"


def perform_single_merge(task: MergeTask, dry_run: bool) -> MergeResult:
    """Perform merge operation for a single project."""
    project_path = task.project_path
    project_name = task.project.path

    if not project_path.exists():
        return MergeResult(project_name, False, "directory not found")

    try:
        repo = git.Repo(project_path)

        # Set up upstream remote
        upstream_url = task.upstream_config.upstream_url
        upstream_rev = task.upstream_config.upstream_rev

        if dry_run:
            return MergeResult(
                project_name,
                True,
                f"would merge {upstream_url}@{upstream_rev} (dry run)"
            )

        # Add/update upstream remote
        try:
            upstream_remote = repo.remote('upstream')
            if upstream_remote.url != upstream_url:
                upstream_remote.set_url(upstream_url)
        except git.exc.InvalidGitRepositoryError:
            repo.create_remote('upstream', upstream_url)

        # Check if shallow and unshallow if needed
        if GitOperations.is_shallow_repo(project_path):
            if not GitOperations.unshallow_repo(project_path):
                return MergeResult(project_name, False, "failed to unshallow repository")

        # Ensure we're on the correct branch
        current_branch = repo.active_branch.name
        target_branch = task.short_revision

        if current_branch != target_branch:
            try:
                # Try to checkout existing branch
                repo.git.checkout(target_branch)
            except git.exc.GitCommandError:
                try:
                    # Create and checkout new branch tracking remote
                    remote_ref = f"{task.repo_remote}/{target_branch}"
                    repo.git.fetch(task.repo_remote)
                    repo.git.checkout('-b', target_branch, remote_ref)
                    branch = repo.heads[target_branch]
                    branch.set_tracking_branch(repo.remotes[task.repo_remote].refs[target_branch])
                except git.exc.GitCommandError as e:
                    return MergeResult(project_name, False, f"failed to checkout branch {target_branch}: {str(e)}")

        # Perform the merge
        try:
            # Fetch from upstream
            upstream_remote = repo.remote('upstream')
            upstream_remote.fetch()

            # Merge with no-rebase and no-edit (equivalent to --no-rebase --no-edit)
            merge_target = f"upstream/{upstream_rev}"
            repo.git.merge(merge_target, no_edit=True)

        except git.exc.GitCommandError as e:
            return MergeResult(project_name, False, f"merge failed: {str(e)}")

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
            lfs_success, lfs_msg = handle_lfs_cleanup(project_path, dry_run)
            if not lfs_success:
                return MergeResult(project_name, False, f"merge succeeded but LFS cleanup failed: {lfs_msg}")

        # Prepare push command for later execution
        push_cmd = f"git push XOS HEAD:{target_branch}"

        return MergeResult(
            project_name,
            True,
            "merge completed successfully",
            needs_push=True,
            push_command=push_cmd,
            had_lfs=had_lfs
        )

    except Exception as e:
        return MergeResult(project_name, False, f"unexpected error: {str(e)}")


class UpstreamMerger:
    def __init__(self, no_reset: bool = False, dry_run: bool = False, max_workers: int = 4):
        self.no_reset = no_reset
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.top = get_android_top()

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        if self.dry_run:
            console.print(f"[blue][DRY RUN] Would run: {command}[/blue]")
            return True

        try:
            # Use Popen for real-time output streaming
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.top,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Stream output in real-time
            for line in iter(process.stdout.readline, ''):
                print(line, end='')

            process.wait()

            if process.returncode != 0:
                console.print(f"\n[red]Command failed with return code {process.returncode}[/red]")
                return False
            return True
        except Exception as e:
            console.print(f"[red]Failed to run {command}: {e}[/red]")
            return False

    def reset_and_sync(self) -> bool:
        """Reset and sync the repo tree."""
        console.print("[yellow]Resetting source tree back to remote state...[/yellow]")
        console.print("[yellow]Any unsaved work will be gone.[/yellow]")

        # Source build/envsetup.sh and run reporeset
        reset_cmd = "source build/envsetup.sh && reporeset"
        if not self.run_repo_command(reset_cmd):
            console.print("[red]Failed to reset repositories[/red]")
            return False

        console.print("[yellow]Syncing repositories...[/yellow]")
        sync_cmd = "source build/envsetup.sh && reposync"
        if not self.run_repo_command(sync_cmd):
            console.print("[red]Failed to sync repositories[/red]")
            return False

        return True

    def generate_manifest(self) -> Path:
        """Generate temporary manifest file."""
        if self.dry_run:
            console.print("[blue][DRY RUN] Would generate temporary manifest[/blue]")
            # Return a dummy path for dry run
            return Path("/tmp/dummy-manifest.xml")

        manifest_path = self.top / "full-manifest.xml"

        try:
            result = subprocess.run(
                ["repo", "manifest"],
                cwd=self.top,
                capture_output=True,
                text=True,
                check=True
            )

            with open(manifest_path, 'w') as f:
                f.write(result.stdout)

            return manifest_path
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to generate manifest: {e}[/red]")
            raise

    def parse_projects_with_upstream(self, manifest_path: Path) -> List[MergeTask]:
        """Parse projects with upstream configuration from manifest."""
        if self.dry_run:
            # Return dummy tasks for dry run
            return [
                MergeTask(
                    ProjectInfo("dummy/path", "dummy-project", "XOS"),
                    Path("/tmp/dummy"),
                    UpstreamConfig("https://example.com/dummy.git", "main"),
                    "XOS",
                    "refs/heads/XOS-16.0",
                    "XOS-16.0"
                )
            ]

        tasks = []

        try:
            tree = ET.parse(manifest_path)
            root = tree.getroot()

            # Get default remote and revision
            default = root.find('default')
            default_remote = default.get('remote', 'XOS') if default is not None else 'XOS'
            default_revision = default.get('revision', 'refs/heads/XOS-16.0') if default is not None else 'refs/heads/XOS-16.0'

            # Build remotes map
            remotes = {}
            for remote in root.findall('remote'):
                remote_name = remote.get('name')
                remote_revision = remote.get('revision', default_revision)
                remotes[remote_name] = remote_revision

            # Process projects with upstream
            for project in root.findall('project[@upstream]'):
                path = project.get('path')
                name = project.get('name')
                upstream_full = project.get('upstream')
                remote = project.get('remote', default_remote)
                revision = project.get('revision')

                if not revision:
                    revision = remotes.get(remote, default_revision)

                short_revision = revision.replace('refs/heads/', '')

                # Parse upstream configuration
                upstream_config = parse_upstream_config(upstream_full, name)

                project_info = ProjectInfo(path, name, remote)
                project_path = self.top / path

                task = MergeTask(
                    project_info,
                    project_path,
                    upstream_config,
                    remote,
                    revision,
                    short_revision
                )

                tasks.append(task)

        except ET.ParseError as e:
            console.print(f"[red]Failed to parse manifest: {e}[/red]")
            raise

        return tasks

    def execute_pushes(self, successful_merges: List[MergeResult]) -> Tuple[int, List[Tuple[str, str]]]:
        """Execute all push operations at the end."""
        if self.dry_run:
            console.print(f"[blue][DRY RUN] Would push {len(successful_merges)} repositories[/blue]")
            return len(successful_merges), []

        console.print(f"\n[bold cyan]Pushing {len(successful_merges)} successfully merged repositories...[/bold cyan]")

        push_successes = 0
        push_failures = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:

            push_task = progress.add_task("[cyan]Pushing changes", total=len(successful_merges))

            for result in successful_merges:
                if not result.needs_push or not result.push_command:
                    progress.update(push_task, advance=1)
                    continue

                try:
                    project_path = self.top / result.project_path

                    # Execute push command
                    push_result = subprocess.run(
                        result.push_command,
                        shell=True,
                        cwd=project_path,
                        capture_output=True,
                        text=True
                    )

                    if push_result.returncode == 0:
                        push_successes += 1
                        console.print(f"[green]✓[/green] {result.project_path}: Pushed successfully")
                    else:
                        push_failures.append((result.project_path, push_result.stderr))
                        console.print(f"[red]✗[/red] {result.project_path}: Push failed: {push_result.stderr}")

                except Exception as e:
                    push_failures.append((result.project_path, str(e)))
                    console.print(f"[red]✗[/red] {result.project_path}: Push exception: {e}")

                progress.update(push_task, advance=1)

        return push_successes, push_failures

    def process_merges(self, tasks: List[MergeTask]) -> Tuple[List[MergeResult], List[MergeResult]]:
        """Process all merge tasks using multi-threading."""
        global executor

        if not tasks:
            console.print("[yellow]No projects with upstream configuration found[/yellow]")
            return [], []

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        console.print(f"\n[bold]{dry_run_prefix}Processing {len(tasks)} upstream merges...[/bold]")

        successful_merges = []
        failed_merges = []

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
                f"[cyan]Merging upstream changes {dry_run_prefix.strip()}",
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
                            successful_merges.append(result)

                            if self.dry_run:
                                console.print(f"[blue]✓[/blue] {result.project_path}: {result.message}")
                            else:
                                lfs_note = " (handled LFS)" if result.had_lfs else ""
                                console.print(f"[green]✓[/green] {result.project_path}: {result.message}{lfs_note}")
                        else:
                            failed_merges.append(result)
                            console.print(f"[red]✗[/red] {result.project_path}: {result.message}")

                        progress.update(merge_task, advance=1)

                    except Exception as e:
                        task = futures[future]
                        failed_result = MergeResult(task.project.path, False, f"exception: {str(e)}")
                        failed_merges.append(failed_result)
                        console.print(f"[red]✗[/red] {task.project.path}: Exception: {e}")
                        progress.update(merge_task, advance=1)

        return successful_merges, failed_merges

    def run(self):
        """Main execution flow."""
        # Prepare repositories if not skipping reset
        if not self.no_reset:
            dry_run_warning = " (DRY RUN MODE - no actual changes will be made)" if self.dry_run else ""
            console.print(f"[yellow]Warning: This will perform a reporeset and a reposync to make{dry_run_warning}[/yellow]")
            console.print("[yellow]sure everything is up to date before doing the merges[/yellow]")
            console.print("[yellow]If you do not want that to happen, use the parameter --no-reset[/yellow]")
            console.print()

            if not self.dry_run:
                try:
                    input("Press ENTER to continue or CTRL+C to abort: ")
                    console.print()
                except KeyboardInterrupt:
                    console.print("\n[red]Aborted by user[/red]")
                    return 1

            if not self.reset_and_sync():
                console.print("[red]Failed to prepare repositories[/red]")
                return 1

        # Generate manifest and parse projects
        try:
            console.print("[cyan]Generating temporary manifest file...[/cyan]")
            manifest_path = self.generate_manifest()

            console.print("[cyan]Parsing projects with upstream configuration...[/cyan]")
            tasks = self.parse_projects_with_upstream(manifest_path)

        except Exception as e:
            console.print(f"[red]Failed to prepare merge tasks: {e}[/red]")
            return 1

        # Process merges
        successful_merges, failed_merges = self.process_merges(tasks)

        # Execute pushes for successful merges (deferred pushing)
        push_successes = 0
        push_failures = []

        if successful_merges and not self.dry_run:
            push_successes, push_failures = self.execute_pushes(successful_merges)

        # Clean up temporary manifest
        if not self.dry_run and manifest_path.exists():
            try:
                manifest_path.unlink()
                console.print("[cyan]Deleted temporary manifest file[/cyan]")
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to delete manifest file: {e}[/yellow]")

        # Summary
        console.print("\n" + "=" * 60)
        console.print("[bold]Upstream merge complete![/bold]")
        console.print(f"[green]Successfully merged:[/green] {len(successful_merges)}/{len(tasks)} projects")

        if not self.dry_run and successful_merges:
            console.print(f"[green]Successfully pushed:[/green] {push_successes}/{len(successful_merges)} projects")

        if failed_merges:
            console.print(f"\n[red]Failed merges ({len(failed_merges)}):[/red]")
            for result in failed_merges:
                console.print(f"  [red]- {result.project_path}:[/red] {result.message}")

        if push_failures:
            console.print(f"\n[red]Failed pushes ({len(push_failures)}):[/red]")
            for project_path, error in push_failures:
                console.print(f"  [red]- {project_path}:[/red] {error}")

        console.print("\n[bold green]Everything done.[/bold green]")

        # Return error code if there were failures
        return 1 if (failed_merges or push_failures) else 0


def main():
    parser = argparse.ArgumentParser(
        description="Merge upstream changes for repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Merge upstream with reset and sync
  %(prog)s --no-reset         # Skip repository reset and sync
  %(prog)s --dry-run          # Show what would be done without making changes
  %(prog)s --workers 2        # Use 2 parallel workers
        """
    )

    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Skip repository reset and sync"
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

    args = parser.parse_args()

    try:
        merger = UpstreamMerger(
            no_reset=args.no_reset,
            dry_run=args.dry_run,
            max_workers=args.workers
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