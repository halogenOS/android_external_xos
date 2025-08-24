#!/usr/bin/env python3
"""
Create snapshot tags for repositories.

Creates timestamped tags for all repositories specified in the manifest snippet,
allowing for easy restoration to a known state.
"""

import os
import sys
import signal
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.console import Console
from dataclasses import dataclass
import threading
import git

# Add xos_common to path
sys.path.insert(0, str(Path(__file__).parent))
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
class TagTask:
    """A single tag creation task."""
    project: ProjectInfo
    project_path: Path
    tag_name: str
    extra_args: List[str]


def process_single_project(task: TagTask, dry_run: bool, remote_name: str) -> Tuple[str, bool, str, str]:
    """Process a single project for tag creation."""
    project_path = task.project_path
    tag_name = task.tag_name
    project_name = task.project.path

    if not project_path.exists():
        return project_name, False, "directory not found", ""

    # Create temporary SnapshotCreator instance for operations
    temp_creator = SnapshotCreator(remote_name=remote_name, dry_run=dry_run)

    # Unshallow if needed
    unshallow_success, unshallow_msg = temp_creator.unshallow_if_needed(project_path)
    if not unshallow_success:
        return project_name, False, f"unshallow failed: {unshallow_msg}", ""

    # Create and push tag
    tag_success, tag_msg = temp_creator.create_and_push_tag(project_path, tag_name, task.extra_args)
    status = "success" if tag_success else "failed"

    return project_name, tag_success, status, tag_msg


class SnapshotCreator:
    def __init__(self, remote_name: str = "XOS", no_reset: bool = False, dry_run: bool = False, max_workers: int = 8):
        self.remote_name = remote_name
        self.no_reset = no_reset
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.top = get_android_top()
        self.snippet_path = self.top / ".repo/manifests/snippets/XOS.xml"

        if not self.snippet_path.exists():
            raise FileNotFoundError(f"Manifest snippet not found: {self.snippet_path}")

        self.manifest = ManifestParser(self.snippet_path)

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        if self.dry_run:
            print(f"[DRY RUN] Would run: {command}")
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
                print(f"\nCommand failed with return code {process.returncode}")
                return False
            return True
        except Exception as e:
            print(f"Failed to run {command}: {e}")
            return False

    def reset_and_sync(self) -> bool:
        """Reset and sync the repo tree."""
        print("Resetting source tree back to remote state...")
        print("Any unsaved work will be gone.")

        # Source build/envsetup.sh and run reporeset
        reset_cmd = "source build/envsetup.sh && reporeset"
        if not self.run_repo_command(reset_cmd):
            print("Failed to reset repositories")
            return False

        print("Syncing repositories...")
        sync_cmd = "source build/envsetup.sh && reposync"
        if not self.run_repo_command(sync_cmd):
            print("Failed to sync repositories")
            return False

        return True

    def generate_tag_name(self, custom_tag: Optional[str] = None,
                         suffix: Optional[str] = None) -> str:
        """Generate tag name based on revision and timestamp."""
        if custom_tag:
            return f"{custom_tag}{suffix or ''}"

        revision = self.manifest.get_remote_revision(self.remote_name)
        if not revision:
            raise ValueError(f"Could not get revision for remote {self.remote_name}")

        # Generate timestamp without unix timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return f"{revision}-{timestamp}{suffix or ''}"

    def unshallow_if_needed(self, repo_path: Path) -> Tuple[bool, str]:
        """Unshallow repository if it's shallow."""
        if GitOperations.is_shallow_repo(repo_path):
            if self.dry_run:
                return True, "would unshallow (dry run)"
            if GitOperations.unshallow_repo(repo_path):
                return True, "unshallowed successfully"
            else:
                return False, "failed to unshallow"
        return True, "not shallow"

    def check_remote_tag_exists(self, repo_path: Path, tag_name: str) -> bool:
        """Check if tag already exists on remote."""
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(self.remote_name)
            # Fetch tags to ensure we have latest info
            remote.fetch(tags=True)
            return f"refs/tags/{tag_name}" in [ref.path for ref in remote.refs]
        except Exception:
            return False

    def create_and_push_tag(self, repo_path: Path, tag_name: str,
                           extra_args: List[str] = None) -> Tuple[bool, str]:
        """Create and push a tag for a repository."""
        # For now, we'll use simple tag creation without message
        # Extra args support can be added later if needed
        message = f"Snapshot tag created at {datetime.now().isoformat()}"

        if self.dry_run:
            if self.check_remote_tag_exists(repo_path, tag_name):
                return True, "already exists (dry run)"
            return True, "would create and push (dry run)"

        # Check if tag already exists on remote
        if self.check_remote_tag_exists(repo_path, tag_name):
            return True, "already exists on remote"

        if GitOperations.create_and_push_tag(repo_path, tag_name, "HEAD",
                                            self.remote_name, message):
            return True, "created and pushed successfully"
        else:
            # Try without message if annotated tag fails
            if GitOperations.create_tag(repo_path, tag_name):
                if GitOperations.push_tag(repo_path, tag_name, self.remote_name):
                    return True, "created and pushed successfully"
            return False, "failed to create/push tag"

    def process_projects(self, tag_name: str, extra_args: List[str] = None, max_workers: int = 8):
        """Process all projects and create tags using multi-threading."""
        global executor

        # Get projects filtered by remote
        projects = self.manifest.get_projects_by_remote(self.remote_name)

        if not projects:
            console.print(f"[red]No projects found with remote '{self.remote_name}'[/red]")
            return

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        console.print(f"\n[bold]{dry_run_prefix}Creating snapshot tag: {tag_name}[/bold]")
        console.print(f"Processing {len(projects)} projects...\n")

        # Prepare tasks
        tasks = []
        for project in projects:
            project_path = self.top / project.path
            tasks.append(TagTask(project, project_path, tag_name, extra_args or []))

        success_count = 0
        failed_projects = []
        already_exists_count = 0

        # Process with progress bar and threading
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:

            task_progress = progress.add_task(
                f"[cyan]Processing projects {dry_run_prefix.strip()}",
                total=len(tasks)
            )

            with ThreadPoolExecutor(max_workers=max_workers) as executor_pool:
                executor = executor_pool

                # Submit all tasks
                futures = {
                    executor_pool.submit(process_single_project, task, self.dry_run, self.remote_name): task
                    for task in tasks
                }

                # Process results as they complete
                for future in as_completed(futures):
                    if stop_event.is_set():
                        executor_pool.shutdown(wait=False, cancel_futures=True)
                        break

                    try:
                        task = futures[future]
                        project_name, success, status, message = future.result()

                        if success:
                            success_count += 1
                            if "already exists" in message:
                                already_exists_count += 1
                                console.print(f"[yellow]✓[/yellow] {project_name}: Tag already exists on remote")
                            elif self.dry_run:
                                console.print(f"[blue]✓[/blue] {project_name}: {message}")
                            else:
                                console.print(f"[green]✓[/green] {project_name}: {message}")
                        else:
                            failed_projects.append((project_name, message))
                            console.print(f"[red]✗[/red] {project_name}: {message}")

                        progress.update(task_progress, advance=1)

                    except Exception as e:
                        task = futures[future]
                        failed_projects.append((task.project.path, f"exception: {str(e)}"))
                        console.print(f"[red]✗[/red] {task.project.path}: Exception: {e}")
                        progress.update(task_progress, advance=1)

        # Summary
        console.print("\n" + "=" * 60)
        console.print(f"[bold]Snapshot creation complete![/bold]")
        console.print(f"[bold]Tag name:[/bold] {tag_name}")
        console.print(f"[green]Successfully tagged:[/green] {success_count}/{len(projects)} projects")

        if already_exists_count > 0:
            console.print(f"[yellow]Already existed on remote:[/yellow] {already_exists_count} projects")

        if failed_projects:
            console.print(f"\n[red]Failed projects ({len(failed_projects)}):[/red]")
            for project_name, error in failed_projects:
                console.print(f"  [red]- {project_name}:[/red] {error}")

        console.print("\n[bold green]Everything done.[/bold green]")

    def run(self, custom_tag: Optional[str] = None,
            tag_suffix: Optional[str] = None,
            extra_git_args: List[str] = None):
        """Main execution flow."""

        # Prepare repositories if not skipping reset
        if not self.no_reset:
            dry_run_warning = " (DRY RUN MODE - no actual changes will be made)" if self.dry_run else ""
            print(f"Warning: This will perform a reporeset and a reposync to make{dry_run_warning}")
            print("sure everything is up to date before creating the snapshot.")
            print("If you do not want that to happen, use the parameter --no-reset")
            print()

            if not self.dry_run:
                response = input("Press ENTER to continue or CTRL+C to abort: ")
                print()

            if not self.reset_and_sync():
                if not self.dry_run:
                    print("Failed to prepare repositories")
                    return 1

        # Generate tag name
        try:
            tag_name = self.generate_tag_name(custom_tag, tag_suffix)
        except ValueError as e:
            print(f"Error: {e}")
            return 1

        # Process all projects
        max_workers = getattr(self, 'max_workers', 8)
        self.process_projects(tag_name, extra_git_args, max_workers)

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Create snapshot tags for repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Create snapshot with auto-generated tag
  %(prog)s --no-reset         # Skip repository reset and sync
  %(prog)s --dry-run          # Show what would be done without making changes
  %(prog)s --workers 4        # Use 4 parallel workers
  %(prog)s my-snapshot        # Use custom tag name
  %(prog)s --suffix -test     # Add suffix to auto-generated tag
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
        default=8,
        help="Number of parallel workers (default: 8)"
    )

    parser.add_argument(
        "--remote",
        default="XOS",
        help="Remote name to use (default: XOS)"
    )

    parser.add_argument(
        "--suffix",
        help="Suffix to append to tag name (env: tag_to_push_suffix)"
    )

    parser.add_argument(
        "tag",
        nargs="?",
        help="Custom tag name (optional)"
    )

    parser.add_argument(
        "--git-args",
        nargs=argparse.REMAINDER,
        help="Additional git tag arguments"
    )

    args = parser.parse_args()

    # Get suffix from environment if not provided
    if not args.suffix:
        args.suffix = os.environ.get("tag_to_push_suffix", "")

    try:
        creator = SnapshotCreator(
            remote_name=args.remote,
            no_reset=args.no_reset,
            dry_run=args.dry_run,
            max_workers=args.workers
        )

        return creator.run(
            custom_tag=args.tag,
            tag_suffix=args.suffix,
            extra_git_args=args.git_args
        )

    except KeyboardInterrupt:
        print("\nAborted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())