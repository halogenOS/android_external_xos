#!/usr/bin/env python3
"""
Merge upstream changes for repositories.

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
from rich.live import Live
from rich.panel import Panel
from rich.console import Group
from rich.table import Table
from dataclasses import dataclass
import threading
import tempfile
import git

from xos_common import (
    ManifestParser,
    GitOperations,
    get_android_top,
    ProjectInfo,
    console,
    handle_lfs_cleanup,
    safe_add_or_update_remote,
    safe_get_remote,
    run_repo_command,
    generate_manifest,
    cleanup_manifest,
    truncate_project_name
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
    merge_info: Optional[Dict[str, Any]] = None


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


# LFS cleanup function is now imported from xos_common


StatusCallback = Optional[callable]


def run_git_with_progress(repo_path: Path, args: list, status_callback: StatusCallback = None) -> Tuple[int, str]:
    """Run a git command, streaming stderr to status_callback in real-time."""
    cmd = ['git', '-C', str(repo_path)] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr_lines = []

    def read_stderr():
        fd = proc.stderr.fileno()
        buf = ""
        while True:
            try:
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                text = chunk.decode('utf-8', errors='replace')
                buf += text
                # Git uses \r for progress updates and \n for final lines
                parts = buf.replace('\r', '\n').split('\n')
                buf = parts[-1]  # Keep incomplete line
                for part in parts[:-1]:
                    line = part.strip()
                    if line:
                        stderr_lines.append(line)
                        if status_callback:
                            status_callback(line)
            except OSError:
                break
        if buf.strip():
            stderr_lines.append(buf.strip())

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()
    proc.wait()
    stderr_thread.join(timeout=5)

    stdout = proc.stdout.read().decode('utf-8', errors='replace') if proc.stdout else ""
    return proc.returncode, "\n".join(stderr_lines)


SLOW_WORKER_THRESHOLD = 10.0  # seconds before a worker gets its own output box


class MergeDisplay:
    """Rich renderable combining a progress bar with per-project output panels."""

    def __init__(self, total: int, description: str = "[cyan]Merging upstream changes"):
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        self.task_id = self.progress.add_task(description, total=total)
        self._workers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        import time as _time
        self._time = _time

    def update_worker(self, project_name: str, step: Optional[str], detail: str = ""):
        with self._lock:
            if step is None:
                self._workers.pop(project_name, None)
            else:
                existing = self._workers.get(project_name)
                now = self._time.monotonic()
                if existing:
                    existing["step"] = step
                    existing["detail"] = detail
                    if detail:
                        lines = existing.setdefault("output_lines", [])
                        lines.append(detail)
                        # Keep last 8 lines of output
                        if len(lines) > 8:
                            existing["output_lines"] = lines[-8:]
                else:
                    entry: Dict[str, Any] = {"step": step, "detail": detail, "started": now, "output_lines": []}
                    if detail:
                        entry["output_lines"].append(detail)
                    self._workers[project_name] = entry

    def advance(self):
        self.progress.advance(self.task_id)

    def __rich__(self):
        now = self._time.monotonic()
        with self._lock:
            workers = {k: dict(v) for k, v in self._workers.items()}

        renderables: list = [self.progress]

        for name in sorted(workers):
            info = workers[name]
            elapsed = now - info.get("started", now)
            step = info["step"]

            if elapsed < SLOW_WORKER_THRESHOLD:
                continue

            elapsed_str = f"{elapsed:.0f}s"
            output_lines = info.get("output_lines", [])
            if output_lines:
                # Show last few lines of git output
                display_lines = []
                for line in output_lines[-6:]:
                    if len(line) > 120:
                        line = "..." + line[-117:]
                    display_lines.append(f"[dim]{line}[/dim]")
                content = "\n".join(display_lines)
            else:
                content = f"[dim]{step}...[/dim]"

            title = f"[cyan]{truncate_project_name(name, 50)}[/cyan] [yellow]{step}[/yellow] [dim]({elapsed_str})[/dim]"
            renderables.append(Panel(content, title=title, border_style="blue", padding=(0, 1)))

        return Group(*renderables)


def perform_single_merge(task: MergeTask, dry_run: bool, display: Optional[MergeDisplay] = None) -> MergeResult:
    """Perform merge operation for a single project."""
    project_path = task.project_path
    project_name = task.project.path

    def status(step: str, detail: str = ""):
        if display:
            display.update_worker(project_name, step, detail)

    def clear_status():
        if display:
            display.update_worker(project_name, None)

    if not project_path.exists():
        return MergeResult(project_name, False, "directory not found")

    try:
        status("initializing")
        repo = git.Repo(project_path)

        # Set up upstream remote
        upstream_url = task.upstream_config.upstream_url
        upstream_rev = task.upstream_config.upstream_rev

        if dry_run:
            # Check current branch and status for better dry-run info
            try:
                current_branch = repo.active_branch.name
            except TypeError:
                # Handle detached HEAD state
                current_branch = "detached HEAD"
            target_branch = task.short_revision

            # Check if repo is shallow
            is_shallow = GitOperations.is_shallow_repo(project_path)
            shallow_info = " (shallow repo - would unshallow)" if is_shallow else ""

            # Check if we need to switch branches
            branch_info = ""
            if current_branch != target_branch:
                branch_info = f" (would checkout {target_branch} from {current_branch})"

            # Check for LFS
            lfsconfig_exists = (project_path / ".lfsconfig").exists()
            gitattributes_has_lfs = False
            gitattributes_path = project_path / ".gitattributes"
            if gitattributes_path.exists():
                with open(gitattributes_path, 'r') as f:
                    gitattributes_has_lfs = 'merge=lfs' in f.read()
            lfs_info = " (has LFS - would cleanup)" if (lfsconfig_exists or gitattributes_has_lfs) else ""

            # Store additional info for table display
            merge_info = {
                "upstream_url": upstream_url,
                "upstream_rev": upstream_rev,
                "target_branch": target_branch,
                "current_branch": current_branch,
                "is_shallow": is_shallow,
                "has_lfs": (lfsconfig_exists or gitattributes_has_lfs),
                "needs_branch_switch": current_branch != target_branch
            }

            return MergeResult(
                project_name,
                True,
                f"would merge {upstream_url}@{upstream_rev}{shallow_info}{branch_info}{lfs_info}",
                merge_info=merge_info
            )

        # Add/update upstream remote
        status("setting up remote")
        upstream_remote = safe_add_or_update_remote(repo, 'upstream', upstream_url)

        # Check if shallow and unshallow if needed
        if GitOperations.is_shallow_repo(project_path):
            status("unshallowing")
            rc, stderr = run_git_with_progress(
                project_path, ['fetch', 'XOS', '--unshallow', '--progress'],
                lambda line: status("unshallowing", line)
            )
            if rc != 0:
                clear_status()
                return MergeResult(project_name, False, f"failed to unshallow repository: {stderr}")
            repo = git.Repo(project_path)

        # Ensure we're on the correct branch
        try:
            current_branch = repo.active_branch.name
        except TypeError:
            # Handle detached HEAD state - we need to checkout the target branch
            current_branch = None

        target_branch = task.short_revision

        if current_branch != target_branch:
            status("checking out branch")
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
                    clear_status()
                    return MergeResult(project_name, False, f"failed to checkout branch {target_branch}: {str(e)}")

        # Fetch from upstream
        status("fetching upstream")
        rc, stderr = run_git_with_progress(
            project_path, ['fetch', 'upstream', '--progress'],
            lambda line: status("fetching upstream", line)
        )
        if rc != 0:
            clear_status()
            return MergeResult(project_name, False, f"fetch failed: {stderr}")

        # Merge
        try:
            status("merging")
            if task.upstream_config.is_tag_or_commit:
                merge_target = upstream_rev
            else:
                merge_target = f"upstream/{upstream_rev}"
            repo.git.merge(merge_target, no_edit=True)

        except git.exc.GitCommandError as e:
            clear_status()
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
            status("cleaning up LFS")
            lfs_success, lfs_msg = handle_lfs_cleanup(project_path, dry_run)
            if not lfs_success:
                clear_status()
                return MergeResult(project_name, False, f"merge succeeded but LFS cleanup failed: {lfs_msg}")

        # Prepare push command for later execution
        push_cmd = f"git push XOS HEAD:{target_branch}"

        # Store merge info for table display
        merge_info = {
            "upstream_url": upstream_url,
            "upstream_rev": upstream_rev,
            "target_branch": target_branch,
            "current_branch": current_branch if current_branch else "detached HEAD",
            "is_shallow": GitOperations.is_shallow_repo(project_path),
            "has_lfs": had_lfs,
            "needs_branch_switch": current_branch != target_branch
        }

        clear_status()
        return MergeResult(
            project_name,
            True,
            "merge completed successfully",
            needs_push=True,
            push_command=push_cmd,
            had_lfs=had_lfs,
            merge_info=merge_info
        )

    except Exception as e:
        clear_status()
        return MergeResult(project_name, False, f"unexpected error: {str(e)}")


class UpstreamMerger:
    def __init__(self, no_reset: bool = False, dry_run: bool = False, max_workers: int = 4, push_only: bool = False):
        self.no_reset = no_reset
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.push_only = push_only
        self.top = get_android_top()

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        return run_repo_command(command, self.top, self.dry_run)

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
        return generate_manifest(self.top, self.dry_run)

    def parse_projects_with_upstream(self, manifest_path: Path) -> List[MergeTask]:
        """Parse projects with upstream configuration from manifest."""
        tasks = []

        try:
            tree = ET.parse(manifest_path)
            root = tree.getroot()

            # Get default remote and revision
            default = root.find('default')
            default_remote = default.get('remote', 'XOS') if default is not None else 'XOS'
            rom_revision = os.environ['ROM_REVISION']
            default_revision = default.get('revision', f'refs/heads/{rom_revision}') if default is not None else f'refs/heads/{rom_revision}'

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

        display = MergeDisplay(
            len(tasks),
            f"[cyan]Merging upstream changes {dry_run_prefix.strip()}"
        )

        with Live(display, refresh_per_second=4, console=console):
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor_pool:
                executor = executor_pool

                futures = {
                    executor_pool.submit(perform_single_merge, task, self.dry_run, display): task
                    for task in tasks
                }

                for future in as_completed(futures):
                    if stop_event.is_set():
                        executor_pool.shutdown(wait=False, cancel_futures=True)
                        break

                    try:
                        task = futures[future]
                        result = future.result()

                        if result.success:
                            successful_merges.append(result)
                        else:
                            failed_merges.append(result)

                        display.advance()

                    except Exception as e:
                        task = futures[future]
                        failed_result = MergeResult(task.project.path, False, f"exception: {str(e)}")
                        failed_merges.append(failed_result)
                        display.advance()

        return successful_merges, failed_merges

    def display_merge_results(self, successful_merges: List[MergeResult], failed_merges: List[MergeResult]):
        """Display merge results in a formatted table."""
        if not successful_merges and not failed_merges:
            return

        # truncate_project_name function is now imported from xos_common

        # Display successful merges
        if successful_merges:
            console.print(f"\n[bold green]Successful merges ({len(successful_merges)}):[/bold green]")

            table = Table(show_header=True, header_style="bold blue")
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Target Branch", style="yellow")
            table.add_column("Current", style="dim")
            table.add_column("Upstream URL", style="green")
            table.add_column("Upstream Rev", style="magenta")
            table.add_column("Notes", style="dim")
            table.add_column("", width=2)  # Status column for emoji

            for result in successful_merges:
                if result.merge_info:
                    info = result.merge_info
                    notes = []

                    if info.get("is_shallow"):
                        notes.append("shallow")
                    if info.get("has_lfs"):
                        notes.append("LFS")
                    if info.get("needs_branch_switch"):
                        current = info.get("current_branch", "detached")
                        if current == "detached HEAD":
                            current = "detached"
                        notes.append(f"from {current}")

                    table.add_row(
                        truncate_project_name(result.project_path),
                        info.get("target_branch", ""),
                        info.get("current_branch", ""),
                        info.get("upstream_url", ""),
                        info.get("upstream_rev", ""),
                        ", ".join(notes) if notes else "",
                        "✅"
                    )
                else:
                    table.add_row(truncate_project_name(result.project_path), "", "", "", "", result.message, "✅")

            console.print(table)

        # Display failed merges in table format as well
        if failed_merges:
            console.print(f"\n[bold red]Failed merges ({len(failed_merges)}):[/bold red]")

            fail_table = Table(show_header=True, header_style="bold red")
            fail_table.add_column("Project", style="cyan", no_wrap=True)
            fail_table.add_column("Error", style="red")
            fail_table.add_column("", width=2)  # Status column for emoji

            for result in failed_merges:
                fail_table.add_row(
                    truncate_project_name(result.project_path),
                    result.message,
                    "❌"
                )

            console.print(fail_table)

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

        # Process merges (skip if push-only mode)
        if self.push_only:
            console.print(f"[blue]Push-only mode: Assuming all {len(tasks)} projects are already merged[/blue]")
            successful_merges = []
            failed_merges = []

            # Create successful merge results for all tasks
            for task in tasks:
                project_name = task.project.path
                project_path = task.project_path
                target_branch = task.short_revision

                if not project_path.exists():
                    continue

                try:
                    repo = git.Repo(project_path)

                    # Check if local HEAD is different from remote HEAD
                    try:
                        local_head = repo.head.commit.hexsha
                        remote_ref = f"{task.repo_remote}/{target_branch}"
                        remote_head = repo.commit(remote_ref).hexsha

                        if local_head == remote_head:
                            # Skip repos that are already up to date
                            continue

                    except git.exc.GitCommandError:
                        # Remote ref doesn't exist, assume we need to push
                        pass

                    push_cmd = f"git push XOS HEAD:{target_branch}"
                    successful_merges.append(MergeResult(
                        project_name,
                        True,
                        "assumed merged (push-only mode)",
                        needs_push=True,
                        push_command=push_cmd
                    ))

                except Exception:
                    # Skip projects that can't be processed
                    continue
        else:
            successful_merges, failed_merges = self.process_merges(tasks)

        # Display results in table format
        self.display_merge_results(successful_merges, failed_merges)

        # Execute pushes for successful merges (deferred pushing)
        push_successes = 0
        push_failures = []

        # Block pushing if any project failed (unless in push-only mode)
        if successful_merges and not self.dry_run:
            if failed_merges and not self.push_only:
                console.print(f"\n[yellow]⚠️  Skipping push because {len(failed_merges)} projects failed to merge.[/yellow]")
                console.print("[yellow]Use --push-only flag to push successful merges after fixing failures.[/yellow]")
            else:
                push_successes, push_failures = self.execute_pushes(successful_merges)

        # Clean up temporary manifest
        cleanup_manifest(manifest_path, self.dry_run)

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

    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Push successful merges even if some projects failed"
    )

    args = parser.parse_args()

    try:
        merger = UpstreamMerger(
            no_reset=args.no_reset,
            dry_run=args.dry_run,
            max_workers=args.workers,
            push_only=args.push_only
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