#!/usr/bin/env python3

import os
import sys
import signal
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.live import Live
from rich.table import Table
from typing import Tuple, List, Dict, Optional
from dataclasses import dataclass
import queue
import threading

from xos_common import (
    get_android_top, get_project_path, ManifestParser,
    GitOperations, ProjectInfo, get_github_token, create_github_repo,
    console
)

# Global executor for signal handling
executor = None
stop_event = threading.Event()

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
class RepoMirrorInfo:
    """Information collected for mirroring a repository."""
    path: str
    branches: List[str]
    tags: List[str]
    error: Optional[str] = None

@dataclass
class PushTask:
    """A single push task."""
    task_type: str  # 'branch' or 'tag'
    repo_path: Path
    path: str
    item: str

def analyze_repo(project: ProjectInfo, repo_revision: str, github_token: Optional[str] = None) -> RepoMirrorInfo:
    """Analyze a single repository and collect information for mirroring."""
    top = get_android_top()
    repo_path = top / project.path

    if not repo_path.exists():
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Path does not exist")

    # Check if shallow repository
    if GitOperations.is_shallow_repo(repo_path):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Shallow repository detected, skipping")

    # Construct remote URLs and repo name
    project_name = get_project_path(project.path)
    xos_url = f"git@git.halogenos.org:halogenOS/{project_name}"
    xosgh_url = f"git@github.com:halogenOS/{project_name}"

    # Create GitHub repo if needed and we have a token
    if github_token:
        create_github_repo(project_name, github_token)

    # Add and fetch XOS remote
    if not GitOperations.add_remote(repo_path, "xos", xos_url):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to add xos remote")

    if not GitOperations.fetch_remote(repo_path, "xos"):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to fetch from xos")

    # Add and fetch XOS GitHub remote
    if not GitOperations.add_remote(repo_path, "xosgh", xosgh_url):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to add xosgh remote")

    if not GitOperations.fetch_remote(repo_path, "xosgh"):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to fetch from xosgh")

    # Collect branches and tags
    branches = GitOperations.get_remote_branches(repo_path, "xos")
    tags = GitOperations.get_tags_matching(repo_path, r'^XOS-[0-9]+\.[0-9]+-.*')

    return RepoMirrorInfo(path=project.path, branches=branches, tags=tags)

def push_item(task: PushTask) -> Tuple[str, str, str, bool, Optional[str]]:
    """Push a single item (branch or tag)."""
    if task.task_type == 'branch':
        success, result = GitOperations.push_branch(task.repo_path, "xos", task.item, "xosgh", task.item)
        if not success and '-' in result and len(result.split('-')[-1]) == 7:
            # Non-fast-forward detected, create a tag instead
            tag_name = result
            commit_ref = f"xos/{task.item}"
            if GitOperations.create_and_push_tag(task.repo_path, tag_name, commit_ref, "xosgh"):
                return task.path, task.item, 'branch->tag', True, tag_name
            else:
                return task.path, task.item, 'branch', False, result
        return task.path, task.item, 'branch', success, result
    else:  # tag
        success = GitOperations.push_tag(task.repo_path, task.item, "xosgh")
        return task.path, task.item, 'tag', success, None

def analysis_worker(projects: List[ProjectInfo], repo_revision: str, github_token: Optional[str],
                   task_queue: queue.Queue, progress, analyze_task) -> int:
    """Worker function for analyzing repositories."""
    skipped = 0

    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(analyze_repo, project, repo_revision, github_token): project
            for project in projects
        }

        for future in as_completed(futures):
            if stop_event.is_set():
                executor.shutdown(wait=False)
                break

            try:
                info = future.result()

                if info.error:
                    console.print(f"[red]✗[/red] {info.path}: {info.error}")
                    skipped += 1
                else:
                    # Queue push tasks
                    top = get_android_top()
                    repo_path = top / info.path

                    for branch in info.branches:
                        task_queue.put(PushTask('branch', repo_path, info.path, branch))

                    for tag in info.tags:
                        task_queue.put(PushTask('tag', repo_path, info.path, tag))

                progress.update(analyze_task, advance=1)

            except Exception as e:
                console.print(f"[red]Error processing result:[/red] {e}")
                skipped += 1
                progress.update(analyze_task, advance=1)

    # Signal that analysis is done
    task_queue.put(None)
    return skipped

def push_worker(task_queue: queue.Queue, progress, push_task, total_items) -> Tuple[int, int]:
    """Worker function for pushing items."""
    successful = 0
    failed = 0
    analysis_done = False

    with ThreadPoolExecutor(max_workers=8) as executor:
        active_futures = set()

        while True:
            # Check for new tasks
            try:
                # Get tasks from queue (with timeout to check stop_event)
                while len(active_futures) < 8 and not analysis_done:
                    task = task_queue.get(timeout=0.1)
                    if task is None:  # Analysis done signal
                        analysis_done = True
                        task_queue.task_done()
                        break

                    future = executor.submit(push_item, task)
                    active_futures.add(future)
                    task_queue.task_done()
            except queue.Empty:
                pass

            # Check completed futures
            done_futures = []
            for future in active_futures:
                if future.done():
                    done_futures.append(future)

            for future in done_futures:
                active_futures.remove(future)
                try:
                    path, item, item_type, success, extra = future.result()

                    if success:
                        successful += 1
                        if item_type == 'branch->tag':
                            console.print(f"[yellow]⚠[/yellow] Branch {item} in {path} was non-fast-forward, created tag {extra} instead")
                    else:
                        failed += 1
                        if extra:
                            console.print(f"[red]✗[/red] Failed to push {item_type} {item} in {path}: {extra}")

                    progress.update(push_task, advance=1,
                                  description=f"[cyan]Pushing to GitHub [green]{successful}[/green]/[red]{failed}[/red]")

                except Exception as e:
                    failed += 1
                    console.print(f"[red]Push error:[/red] {e}")
                    progress.update(push_task, advance=1)

            # Check if we should stop
            if stop_event.is_set():
                executor.shutdown(wait=False)
                break

            # If no tasks and no active futures, and analysis is done, we're finished
            if not active_futures and analysis_done:
                break

            # Small sleep to prevent busy waiting
            if not active_futures and not analysis_done:
                threading.Event().wait(0.1)

    return successful, failed

def perform_repo_sync(args):
    """Perform repo sync using the repo tool."""
    import subprocess

    console.print("\n[bold]Running reporeset...[/bold]")
    # Reset manifest first
    manifest_dir = Path('.repo/manifests')
    if manifest_dir.exists():
        try:
            import git
            manifest_repo = git.Repo(manifest_dir)
            rom_revision = os.environ['ROM_REVISION']
            manifest_repo.remotes.origin.fetch()
            manifest_repo.head.reference = manifest_repo.remotes.origin.refs[rom_revision]
            manifest_repo.head.reset(index=True, working_tree=True)
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to reset manifest: {e}")

    # Use repo tool for sync
    result = subprocess.run(
        ['repo', 'sync', '--force-sync', '-c', '--no-clone-bundle',
         '--no-tags', '-j', str(args.jobs)],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        console.print(f"[red]Error:[/red] reposync failed\n{result.stderr}")
        return False

    console.print("[green]Sync completed successfully[/green]")
    return True

def main():
    parser = argparse.ArgumentParser(
        description='Mirror all repositories to GitHub'
    )
    parser.add_argument('--no-reset', action='store_true',
                       help='Skip repo reset and sync')
    parser.add_argument('--workers', type=int, default=4,
                       help='Number of parallel workers (default: 4)')
    parser.add_argument('--jobs', type=int, default=4,
                       help='Number of sync jobs for repo tool (default: 4)')
    args = parser.parse_args()

    # Check environment
    try:
        top = get_android_top()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    os.chdir(top)

    # Get manifest snippet path
    snippet_path = top / '.repo' / 'manifests' / 'snippets' / 'XOS.xml'
    if not snippet_path.exists():
        console.print(f"[red]Error:[/red] Manifest snippet not found at {snippet_path}")
        sys.exit(1)

    # Perform reset and sync if not skipped
    if not args.no_reset:
        console.print("[yellow]Warning:[/yellow] This will perform a reporeset and a reposync to make sure")
        console.print("everything is up to date before doing the merges")
        console.print("If you do not want that to happen, abort now using CTRL+C")
        console.print("and use the parameter --no-reset")
        console.print("Otherwise, just confirm with ENTER")
        try:
            input()
        except KeyboardInterrupt:
            console.print("\n[red]Aborted.[/red]")
            sys.exit(0)

        if not perform_repo_sync(args):
            sys.exit(1)

    # Parse manifest
    manifest = ManifestParser(snippet_path)

    # Get repo revision
    repo_revision = manifest.get_remote_revision("XOS")
    if not repo_revision:
        console.print("[red]Error:[/red] Could not determine repo revision from manifest")
        sys.exit(1)

    console.print(f"\n[bold]Using revision:[/bold] {repo_revision}")

    # Get all projects
    projects = manifest.get_projects()
    if not projects:
        console.print("[red]Error:[/red] No projects found in manifest")
        sys.exit(1)

    console.print(f"[bold]Found {len(projects)} projects to analyze[/bold]")

    # Get GitHub token for repo creation
    github_token = get_github_token()
    if not github_token:
        console.print("[yellow]Warning:[/yellow] No GitHub token found. Repository creation will be skipped.")
        console.print("Place your token in ~/.creds/xos_github_token to enable repo creation.")

    # Create task queue for communication between workers
    task_queue = queue.Queue(maxsize=100)

    # Estimate total items (rough estimate)
    estimated_items_per_repo = 20
    estimated_total = len(projects) * estimated_items_per_repo

    # Run analysis and pushing in parallel with dual progress bars
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:

        analyze_task = progress.add_task("[cyan]Analyzing repositories", total=len(projects))
        push_task = progress.add_task("[cyan]Pushing to GitHub", total=estimated_total)

        # Start analysis in a thread
        skipped_count = [0]  # Use list to capture value from thread
        analysis_thread = threading.Thread(
            target=lambda: skipped_count.__setitem__(0, analysis_worker(projects, repo_revision, github_token, task_queue, progress, analyze_task))
        )
        analysis_thread.start()

        # Start pushing in main thread
        successful, failed = push_worker(task_queue, progress, push_task, estimated_total)

        # Wait for analysis to complete
        analysis_thread.join()

        # Update push task total with actual count
        actual_total = progress.tasks[push_task].completed
        progress.update(push_task, total=actual_total)

    # Summary
    console.print(f"\n[bold]Complete:[/bold]")
    console.print(f"  [green]Successful pushes:[/green] {successful}")
    console.print(f"  [red]Failed pushes:[/red] {failed}")
    console.print(f"  [bold]Total:[/bold] {successful + failed}")

    console.print("\n[bold green]Everything done.[/bold green]")
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
