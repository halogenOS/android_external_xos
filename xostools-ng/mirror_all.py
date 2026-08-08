#!/usr/bin/env python3

import os
import re
import sys
import signal
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.console import Group
from rich.live import Live
from rich.table import Table
from typing import Tuple, List, Dict, Optional
from dataclasses import dataclass, field
import queue
import threading
import time

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
    # Diverged branches, as (branch, tag name) — mirrored as a tag, not a branch.
    fallback_tags: List[Tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None
    warning: Optional[str] = None  # partial problem; the rest is still mirrorable
    up_to_date: int = 0  # items already identical on the mirror, not pushed

@dataclass
class PushTask:
    """A single push task."""
    task_type: str  # 'branch', 'tag' or 'diverged-tag'
    repo_path: Path
    path: str
    item: str
    origin_branch: Optional[str] = None  # for 'diverged-tag': the branch it stands in for
    index: int = 1  # position within its repository's job, for the status view
    count: int = 1
    note: Optional[str] = None  # live detail for the status view, set while pushing

@dataclass
class RepoPushJob:
    """Every ref one repository has to mirror.

    Refs of the same repository are pushed one after another rather than in
    parallel: they nearly always share objects, so concurrent pushes negotiate
    and upload the same packs several times over.
    """
    path: str
    tasks: List[PushTask]

class MirrorStats:
    """Counters and in-flight work, shared between the analysis and push side."""

    def __init__(self):
        self._lock = threading.Lock()
        self.analyzed = 0
        self.repos_total = 0
        self.repos_failed = 0
        self.repos_partial = 0
        self.branches_queued = 0
        self.tags_queued = 0
        self.up_to_date = 0
        self.pushed = 0
        self.failed = 0
        self.retagged = 0
        self.analyzing: Optional[str] = None
        self.inflight: Dict[int, Tuple[PushTask, float]] = {}
        self._next_id = 0

    def record_analysis(self, info: RepoMirrorInfo):
        with self._lock:
            self.analyzed += 1
            self.analyzing = info.path
            if info.error:
                self.repos_failed += 1
            else:
                if info.warning:
                    self.repos_partial += 1
                self.branches_queued += len(info.branches)
                self.tags_queued += len(info.tags) + len(info.fallback_tags)
                self.up_to_date += info.up_to_date

    def start_push(self, task: PushTask) -> int:
        with self._lock:
            self._next_id += 1
            self.inflight[self._next_id] = (task, time.monotonic())
            return self._next_id

    def finish_push(self, push_id: int, success: bool, retagged: bool = False):
        with self._lock:
            self.inflight.pop(push_id, None)
            if success:
                self.pushed += 1
            else:
                self.failed += 1
            if retagged:
                self.retagged += 1

    def snapshot(self) -> Tuple[dict, List[Tuple[PushTask, float]]]:
        with self._lock:
            counters = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
            inflight = sorted(self.inflight.values(), key=lambda entry: entry[1])
        return counters, inflight

def format_duration(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

def render_status(stats: MirrorStats, max_rows: int = 8) -> Table:
    """Live view of the counters and of every push currently on the wire."""
    counters, inflight = stats.snapshot()

    grid = Table.grid(padding=(0, 1))
    grid.add_column()

    queued = counters['branches_queued'] + counters['tags_queued']
    grid.add_row(
        f"[bold]Queued[/bold] {queued} "
        f"([cyan]{counters['branches_queued']}[/cyan] branches, "
        f"[cyan]{counters['tags_queued']}[/cyan] tags) · "
        f"[green]{counters['pushed']} pushed[/green] · "
        f"[red]{counters['failed']} failed[/red] · "
        f"[yellow]{counters['retagged']} retagged[/yellow] · "
        f"[dim]{counters['up_to_date']} already current[/dim]"
    )
    grid.add_row(
        f"[bold]Repos[/bold] {counters['analyzed']}/{counters['repos_total']} analyzed · "
        f"[red]{counters['repos_failed']} failed[/red] · "
        f"[yellow]{counters['repos_partial']} partial[/yellow] · "
        f"[dim]last:[/dim] {counters['analyzing'] or '-'}"
    )

    if inflight:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        table.add_column(style="dim", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)

        now = time.monotonic()
        for task, started in inflight[:max_rows]:
            detail = f"{task.task_type} {task.item}"
            if task.note:
                detail += f" [yellow]— {task.note}[/yellow]"
            table.add_row("  ↑", task.path, f"{task.index}/{task.count}", detail,
                          format_duration(now - started))

        if len(inflight) > max_rows:
            table.add_row("", f"[dim]… and {len(inflight) - max_rows} more[/dim]", "", "", "")

        grid.add_row(table)

    return grid

def analyze_repo(project: ProjectInfo, repo_revision: str, github_token: Optional[str] = None,
                 skip_up_to_date: bool = True) -> RepoMirrorInfo:
    """Analyze a single repository and collect information for mirroring."""
    top = get_android_top()
    repo_path = top / project.path

    if not repo_path.exists():
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Path does not exist")

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

    if not GitOperations.fetch_remote(repo_path, "xos", prune=True):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to fetch from xos")

    # Add and fetch XOS GitHub remote
    if not GitOperations.add_remote(repo_path, "xosgh", xosgh_url):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to add xosgh remote")

    if not GitOperations.fetch_remote(repo_path, "xosgh", prune=True):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to fetch from xosgh")

    # Collect what the source side has. Both branches and tags come from the
    # xos remote, never from local refs: this is a mirror, so anything that is
    # not published on XOS must not appear on the GitHub mirror either.
    branches = GitOperations.get_remote_branches(repo_path, "xos")

    if not GitOperations.fetch_remote_tags(repo_path, "xos"):
        return RepoMirrorInfo(path=project.path, branches=[], tags=[],
                             error="Failed to fetch tags from xos")

    tag_pattern = re.compile(r'^XOS-[0-9]+\.[0-9]+-.*')
    tags = [t for t in GitOperations.get_remote_tag_shas(repo_path, "xos") if tag_pattern.match(t)]

    # A truncated history can only be pushed as far as its boundary, so drop the
    # refs that actually reach one instead of the whole repository: a tree that
    # was ever fetched shallowly keeps stale shallow entries forever, and those
    # usually sit on history no XOS ref even reaches.
    warning = None
    truncated = GitOperations.get_truncated_commits(repo_path)
    if truncated:
        branch_prefix = "refs/remotes/xos/"
        tag_prefix = GitOperations.remote_tag_ref("xos", "")
        blocked_branches, blocked_tags = set(), set()

        for commit in truncated:
            for ref in GitOperations.refs_containing(repo_path, commit, branch_prefix):
                blocked_branches.add(ref[len(branch_prefix):])
            for ref in GitOperations.refs_containing(repo_path, commit, tag_prefix):
                blocked_tags.add(ref[len(tag_prefix):])

        branches = [b for b in branches if b not in blocked_branches]
        tags = [t for t in tags if t not in blocked_tags]

        if blocked_branches or blocked_tags:
            blocked = sorted(blocked_branches) + sorted(blocked_tags)
            warning = f"Truncated history, not mirroring: {', '.join(blocked)}"

    if not skip_up_to_date:
        return RepoMirrorInfo(path=project.path, branches=branches, tags=tags,
                              warning=warning)

    # Tags are immutable here (an existing name is never re-pushed), so their
    # names on the mirror are enough to decide.
    mirrored_tags = {}
    pending_tags = tags
    if GitOperations.fetch_remote_tags(repo_path, "xosgh"):
        mirrored_tags = GitOperations.get_remote_tag_shas(repo_path, "xosgh")
        pending_tags = [t for t in tags if t not in mirrored_tags]

    # Both remotes have just been fetched, so the remote-tracking refs are an
    # accurate picture of either side and every branch can be classified here,
    # without a push attempt: identical, fast-forwardable, or diverged. A
    # diverged branch cannot be mirrored as a branch and is preserved under a
    # name derived from its commit, so once that tag exists there is nothing
    # left to do and the run must stop rediscovering it by failing a push.
    source = GitOperations.get_remote_branch_shas(repo_path, "xos")
    mirror = GitOperations.get_remote_branch_shas(repo_path, "xosgh")

    pending_branches = []
    fallback_tags = []
    up_to_date = len(tags) - len(pending_tags)

    for branch in branches:
        source_sha = source.get(branch)
        mirror_sha = mirror.get(branch)

        if source_sha == mirror_sha:
            up_to_date += 1
        elif mirror_sha is None or GitOperations.is_ancestor(repo_path, mirror_sha, source_sha):
            pending_branches.append(branch)
        else:
            tag_name = f"{branch}-{source_sha[:7]}"
            if tag_name in mirrored_tags:
                up_to_date += 1
            else:
                fallback_tags.append((branch, tag_name))

    return RepoMirrorInfo(path=project.path, branches=pending_branches,
                          tags=pending_tags, fallback_tags=fallback_tags,
                          up_to_date=up_to_date, warning=warning)

def push_item(task: PushTask) -> Tuple[str, str, str, bool, Optional[str]]:
    """Push a single item (branch or tag)."""
    if task.task_type == 'diverged-tag':
        # Already known to have diverged, so go straight to the tag.
        source_ref = f"refs/remotes/xos/{task.origin_branch}"
        success = GitOperations.push_tag_from_ref(task.repo_path, source_ref, task.item, "xosgh")
        return task.path, task.origin_branch, 'branch->tag', success, task.item

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

        if not success and GitOperations.is_pack_too_big(result):
            def report(pushed, total, chunk):
                task.note = f"chunked {pushed}/{total} commits, next {chunk}"

            task.note = "too big, walking history"
            success, result = GitOperations.push_branch_in_chunks(
                task.repo_path, "xos", task.item, "xosgh", task.item, on_progress=report)
            task.note = None
            return task.path, task.item, 'branch (chunked)', success, result

        return task.path, task.item, 'branch', success, result
    else:  # tag
        source_ref = GitOperations.remote_tag_ref("xos", task.item)
        success = GitOperations.push_tag_from_ref(task.repo_path, source_ref, task.item, "xosgh")
        return task.path, task.item, 'tag', success, None

def push_repo(job: RepoPushJob, stats: MirrorStats, progress, push_task) -> Tuple[int, int]:
    """Push one repository's refs sequentially, reporting each as it lands."""
    successful = 0
    failed = 0

    for task in job.tasks:
        if stop_event.is_set():
            break

        push_id = stats.start_push(task)
        try:
            path, item, item_type, success, extra = push_item(task)
            retagged = item_type == 'branch->tag'

            if success:
                successful += 1
                if retagged:
                    console.print(f"[yellow]⚠[/yellow] Branch {item} in {path} was non-fast-forward, created tag {extra} instead")
                elif item_type == 'branch (chunked)':
                    console.print(f"[yellow]⚠[/yellow] Branch {item} in {path} was too big for one push: {extra}")
            else:
                failed += 1
                if extra:
                    console.print(f"[red]✗[/red] Failed to push {item_type} {item} in {path}: {extra}")

            stats.finish_push(push_id, success, retagged)

        except Exception as e:
            failed += 1
            console.print(f"[red]Push error:[/red] {job.path} {task.item}: {e}")
            stats.finish_push(push_id, False)

        progress.update(push_task, advance=1)

    return successful, failed

def analysis_worker(projects: List[ProjectInfo], repo_revision: str, github_token: Optional[str],
                   task_queue: queue.Queue, progress, analyze_task, push_task,
                   skip_up_to_date: bool, stats: MirrorStats) -> int:
    """Worker function for analyzing repositories."""
    skipped = 0

    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(analyze_repo, project, repo_revision, github_token, skip_up_to_date): project
            for project in projects
        }

        for future in as_completed(futures):
            if stop_event.is_set():
                executor.shutdown(wait=False)
                break

            try:
                info = future.result()
                stats.record_analysis(info)

                if info.error:
                    console.print(f"[red]✗[/red] {info.path}: {info.error}")
                    skipped += 1
                else:
                    if info.warning:
                        console.print(f"[yellow]⚠[/yellow] {info.path}: {info.warning}")

                    # Queue the repository's refs as one job
                    top = get_android_top()
                    repo_path = top / info.path

                    items = ([('branch', b, None) for b in info.branches] +
                             [('tag', t, None) for t in info.tags] +
                             [('diverged-tag', tag, branch)
                              for branch, tag in info.fallback_tags])
                    tasks = [PushTask(kind, repo_path, info.path, item, origin,
                                      index, len(items))
                             for index, (kind, item, origin) in enumerate(items, start=1)]

                    if tasks:
                        task_queue.put(RepoPushJob(info.path, tasks))

                # The real amount of work is only known as the analysis uncovers
                # it, so grow the push total instead of guessing it up front.
                progress.update(push_task,
                                total=stats.branches_queued + stats.tags_queued)
                progress.update(analyze_task, advance=1,
                                description=f"[cyan]Analyzing [dim]{info.path}[/dim]")

            except Exception as e:
                console.print(f"[red]Error processing result:[/red] {e}")
                skipped += 1
                progress.update(analyze_task, advance=1)

    # Signal that analysis is done
    task_queue.put(None)
    return skipped

def push_worker(task_queue: queue.Queue, progress, push_task, stats: MirrorStats) -> Tuple[int, int]:
    """Worker function for pushing items."""
    successful = 0
    failed = 0
    analysis_done = False

    with ThreadPoolExecutor(max_workers=8) as executor:
        active_futures = set()

        while True:
            # Check for new jobs. One job is one repository, so the pool runs
            # eight different repositories at once and never one twice.
            try:
                # Get jobs from queue (with timeout to check stop_event)
                while len(active_futures) < 8 and not analysis_done:
                    job = task_queue.get(timeout=0.1)
                    if job is None:  # Analysis done signal
                        analysis_done = True
                        task_queue.task_done()
                        break

                    active_futures.add(executor.submit(push_repo, job, stats, progress, push_task))
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
                    repo_successful, repo_failed = future.result()
                    successful += repo_successful
                    failed += repo_failed

                except Exception as e:
                    console.print(f"[red]Push error:[/red] {e}")

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

def should_skip_vendor_repo(project_path: str) -> bool:
    """Check if vendor repo should be skipped based on path components."""
    if not project_path.startswith('vendor/'):
        return False

    # Count path components: vendor/nothing/Pong has 3 components
    path_components = project_path.split('/')
    return len(path_components) > 2

def parse_local_manifests(top: Path) -> List[ProjectInfo]:
    """Parse all local manifest files and return projects."""
    local_manifests_dir = top / '.repo' / 'local_manifests'
    projects = []

    if not local_manifests_dir.exists():
        return projects

    # Find all XML files in local_manifests directory
    xml_files = list(local_manifests_dir.glob('*.xml'))

    for xml_file in xml_files:
        try:
            local_manifest = ManifestParser(xml_file)

            # Filter for XOS remote projects only
            xos_projects = local_manifest.get_projects_by_remote("XOS")

            # Filter out vendor repos with more than 2 path components
            filtered_projects = []
            skipped_count = 0
            for project in xos_projects:
                if should_skip_vendor_repo(project.path):
                    console.print(f"[yellow]Skipping vendor repo with >2 components: {project.path}[/yellow]")
                    skipped_count += 1
                else:
                    filtered_projects.append(project)

            projects.extend(filtered_projects)

            if filtered_projects:
                console.print(f"[cyan]Found {len(filtered_projects)} XOS projects in {xml_file.name}[/cyan]")
            if skipped_count:
                console.print(f"[yellow]Skipped {skipped_count} vendor repos in {xml_file.name}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to parse {xml_file.name}: {e}")

    return projects

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
    parser.add_argument('--push-all', action='store_true',
                       help='Push every branch and tag, even the ones already up to date on the mirror')
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

    # Parse main manifest
    manifest = ManifestParser(snippet_path)

    # Get repo revision
    repo_revision = manifest.get_remote_revision("XOS")
    if not repo_revision:
        console.print("[red]Error:[/red] Could not determine repo revision from manifest")
        sys.exit(1)

    console.print(f"\n[bold]Using revision:[/bold] {repo_revision}")

    # Get projects from main manifest (XOS remote only)
    main_projects = manifest.get_projects_by_remote("XOS")
    if not main_projects:
        console.print("[red]Error:[/red] No XOS projects found in main manifest")
        sys.exit(1)

    console.print(f"[bold]Found {len(main_projects)} XOS projects in main manifest[/bold]")

    # Get projects from local manifests
    local_projects = parse_local_manifests(top)
    console.print(f"[bold]Found {len(local_projects)} projects in local manifests[/bold]")

    # Combine projects and remove duplicates based on path
    all_projects = main_projects + local_projects
    unique_projects = {}
    for project in all_projects:
        if project.path not in unique_projects:
            unique_projects[project.path] = project
        else:
            console.print(f"[yellow]Duplicate project path detected, using first occurrence: {project.path}[/yellow]")

    projects = list(unique_projects.values())
    console.print(f"[bold]Total unique projects to analyze: {len(projects)}[/bold]")

    # Get GitHub token for repo creation
    github_token = get_github_token()
    if not github_token:
        console.print("[yellow]Warning:[/yellow] No GitHub token found. Repository creation will be skipped.")
        console.print("Place your token in ~/.creds/xos_github_token to enable repo creation.")

    # Create task queue for communication between workers
    task_queue = queue.Queue(maxsize=100)

    stats = MirrorStats()
    stats.repos_total = len(projects)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console
    )

    analyze_task = progress.add_task("[cyan]Analyzing repositories", total=len(projects))
    # The push total starts unknown and grows as the analysis queues work.
    push_task = progress.add_task("[cyan]Pushing to GitHub", total=0)

    # Run analysis and pushing in parallel, below a live status panel
    with Live(console=console, refresh_per_second=4, transient=False) as live:
        def refresh():
            live.update(Group(progress.get_renderable(), render_status(stats)))

        def refresh_loop():
            # Elapsed times of in-flight pushes have to tick on their own, so
            # redraw on a timer rather than only when something completes.
            while not stop_refresh.wait(0.25):
                refresh()

        stop_refresh = threading.Event()
        refresh_thread = threading.Thread(target=refresh_loop)
        refresh_thread.start()

        # Start analysis in a thread
        skipped_count = [0]  # Use list to capture value from thread
        analysis_thread = threading.Thread(
            target=lambda: skipped_count.__setitem__(0, analysis_worker(
                projects, repo_revision, github_token, task_queue, progress, analyze_task,
                push_task, not args.push_all, stats))
        )
        analysis_thread.start()

        # Start pushing in main thread
        successful, failed = push_worker(task_queue, progress, push_task, stats)

        # Wait for analysis to complete
        analysis_thread.join()

        stop_refresh.set()
        refresh_thread.join()
        refresh()

    # Summary
    console.print(f"\n[bold]Complete:[/bold]")
    console.print(f"  [green]Successful pushes:[/green] {successful}")
    console.print(f"  [red]Failed pushes:[/red] {failed}")
    console.print(f"  [yellow]Non-fast-forward, mirrored as tag:[/yellow] {stats.retagged}")
    if stats.up_to_date:
        console.print(f"  [dim]Already up to date (not pushed):[/dim] {stats.up_to_date}")
    if stats.repos_failed or stats.repos_partial:
        console.print(f"  [red]Repositories skipped:[/red] {stats.repos_failed}"
                      f"  [yellow]partially mirrored:[/yellow] {stats.repos_partial}")
    console.print(f"  [bold]Total:[/bold] {successful + failed}")

    console.print("\n[bold green]Everything done.[/bold green]")
    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
