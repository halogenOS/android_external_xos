#!/usr/bin/env python3
"""
List differences between local XOS branches and their upstream/remote counterparts.

For each XOS project, detects the upstream type and shows commits ahead/behind:
- merge-aosp projects: compared against the AOSP tag
- upstream projects: compared against the upstream URL/branch
- pure XOS projects: compared against the XOS remote branch
"""

import os
import sys
import signal
import argparse
import xml.etree.ElementTree as ET
import threading
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.live import Live
from rich.console import Group
import git

from xos_common import (
    get_android_top,
    ProjectInfo,
    console,
    generate_manifest,
    cleanup_manifest,
    truncate_project_name,
)

stop_event = threading.Event()
executor = None


def signal_handler(signum, frame):
    console.print("\n[red]Interrupted![/red]")
    stop_event.set()
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)
    sys.exit(1)


signal.signal(signal.SIGINT, signal_handler)


@dataclass
class UpstreamInfo:
    """Describes what to compare a project against."""
    url: str
    ref: str
    is_tag_or_commit: bool = False
    kind: str = "upstream"  # "aosp", "upstream", "xos"


@dataclass
class DiffResult:
    project_path: str
    kind: str
    upstream_ref: str
    ahead: int
    behind: int
    error: Optional[str] = None
    ahead_commits: Optional[List[str]] = None   # oneline summaries of local-only commits
    behind_commits: Optional[List[str]] = None   # oneline summaries of remote-only commits
    dirty_stat: Optional[str] = None             # git diff --stat output



@dataclass
class DiffTask:
    project: ProjectInfo
    project_path: Path
    upstream: UpstreamInfo
    local_branch: str


def build_tasks(top: Path, manifest_path: Path) -> List[DiffTask]:
    """Parse manifest and build diff tasks for all projects.

    Uses the generated manifest as the single source of truth for remote names
    and revisions. The XOS snippet is only used to classify projects by kind.
    """
    tasks = []

    # Parse generated manifest
    manifest_tree = ET.parse(manifest_path)
    manifest_root = manifest_tree.getroot()

    # Get defaults from manifest
    default = manifest_root.find('default')
    default_remote = default.get('remote', '') if default is not None else ''
    default_revision = default.get('revision', '') if default is not None else ''

    # Build remotes map for revision fallback
    remotes = {}
    for remote in manifest_root.findall('remote'):
        rname = remote.get('name')
        rrev = remote.get('revision', default_revision)
        remotes[rname] = rrev

    # Parse XOS snippet only for classification (merge-aosp, upstream, pure)
    snippet_path = top / "manifest" / "snippets" / "XOS.xml"
    xos_projects: Dict[str, ET.Element] = {}
    if snippet_path.exists():
        xos_tree = ET.parse(snippet_path)
        for proj in xos_tree.getroot().findall('project'):
            path = proj.get('path')
            if path:
                xos_projects[path] = proj

    # Process all projects in manifest
    for project in manifest_root.findall('project'):
        path = project.get('path')
        name = project.get('name')
        remote = project.get('remote', default_remote)

        if not path or not name:
            continue

        revision = project.get('revision')
        if not revision:
            revision = remotes.get(remote, default_revision)
        is_tag = revision.startswith('refs/tags/')
        ref = revision.replace('refs/heads/', '').replace('refs/tags/', '')

        # Classify by presence in XOS snippet
        xos_proj = xos_projects.get(path)
        if xos_proj is not None:
            if xos_proj.get('merge-aosp') == 'true':
                kind = "aosp"
            elif xos_proj.get('upstream'):
                kind = "upstream"
            else:
                kind = "xos"
        else:
            kind = "untracked"

        project_path = top / path
        tasks.append(DiffTask(
            ProjectInfo(path, name, remote),
            project_path,
            UpstreamInfo("", ref, is_tag_or_commit=is_tag, kind=kind),
            ref,
        ))

    return tasks


def compute_diff(task: DiffTask, fetch: bool) -> DiffResult:
    """Compute ahead/behind counts for a single project."""
    if not task.project_path.exists():
        return DiffResult(task.project.path, task.upstream.kind, task.upstream.ref, 0, 0, "not found")

    try:
        repo = git.Repo(task.project_path)
    except Exception as e:
        return DiffResult(task.project.path, task.upstream.kind, task.upstream.ref, 0, 0, str(e))

    upstream = task.upstream
    remote_name = task.project.remote
    if upstream.is_tag_or_commit:
        compare_ref = upstream.ref
    else:
        compare_ref = f"{remote_name}/{upstream.ref}"

    if fetch:
        try:
            repo.remotes[remote_name].fetch(upstream.ref)
        except Exception as e:
            return DiffResult(task.project.path, upstream.kind, upstream.ref, 0, 0, f"fetch failed: {e}")

    try:
        # Compute ahead/behind
        behind, ahead = repo.git.rev_list(
            '--left-right', '--count', f'{compare_ref}...HEAD'
        ).split()
        ahead, behind = int(ahead), int(behind)

        # Collect oneline commit summaries
        ahead_commits = None
        behind_commits = None
        if ahead > 0:
            ahead_commits = repo.git.log('--oneline', f'{compare_ref}..HEAD').splitlines()
        if behind > 0:
            behind_commits = repo.git.log('--oneline', f'HEAD..{compare_ref}').splitlines()

        # Collect uncommitted changes
        dirty_stat = None
        diff_output = repo.git.diff('--stat')
        if diff_output:
            dirty_stat = diff_output

        return DiffResult(task.project.path, upstream.kind, upstream.ref, ahead, behind,
                          ahead_commits=ahead_commits, behind_commits=behind_commits,
                          dirty_stat=dirty_stat)

    except git.exc.GitCommandError as e:
        return DiffResult(task.project.path, upstream.kind, upstream.ref, 0, 0, str(e))
    except Exception as e:
        return DiffResult(task.project.path, upstream.kind, upstream.ref, 0, 0, str(e))


class DiffDisplay:
    """Progress display for diff computation."""

    def __init__(self, total: int):
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        self.task_id = self.progress.add_task("[cyan]Comparing branches", total=total)

    def advance(self):
        self.progress.advance(self.task_id)

    def __rich__(self):
        return Group(self.progress)


def main():
    parser = argparse.ArgumentParser(
        description="List differences between local XOS branches and their upstream counterparts",
    )
    parser.add_argument("--fetch", action="store_true",
                        help="Fetch from remotes before comparing (slower but accurate)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Number of parallel workers (default: 8)")
    parser.add_argument("--all", action="store_true",
                        help="Show all projects, including those with no differences")
    parser.add_argument("--kind", choices=["aosp", "upstream", "xos", "untracked"],
                        help="Only show projects of this upstream kind")
    args = parser.parse_args()

    top = get_android_top()

    console.print("[cyan]Generating manifest...[/cyan]")
    manifest_path = generate_manifest(top)

    console.print("[cyan]Parsing projects...[/cyan]")
    tasks = build_tasks(top, manifest_path)
    console.print(f"[cyan]Found {len(tasks)} XOS projects[/cyan]")

    if args.kind:
        tasks = [t for t in tasks if t.upstream.kind == args.kind]
        console.print(f"[cyan]Filtered to {len(tasks)} {args.kind} projects[/cyan]")

    # Compute diffs
    global executor
    results: List[DiffResult] = []
    display = DiffDisplay(len(tasks))

    with Live(display, refresh_per_second=4, console=console):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            executor = pool
            futures = {pool.submit(compute_diff, task, args.fetch): task for task in tasks}

            for future in as_completed(futures):
                if stop_event.is_set():
                    break
                try:
                    results.append(future.result())
                except Exception as e:
                    task = futures[future]
                    results.append(DiffResult(task.project.path, task.upstream.kind,
                                             task.upstream.ref, 0, 0, str(e)))
                display.advance()

    cleanup_manifest(manifest_path)

    # Filter and sort
    if not args.all:
        results = [r for r in results if r.ahead or r.behind or r.error or r.dirty_stat]

    results.sort(key=lambda r: (r.kind, r.project_path))

    if not results:
        console.print("\n[green]All projects are in sync with their upstreams.[/green]")
        return 0

    # Display results grouped by kind
    kind_labels = {"aosp": "AOSP forks", "upstream": "Upstream-tracked", "xos": "Pure XOS", "untracked": "Untracked AOSP"}

    for kind in ("aosp", "upstream", "xos", "untracked"):
        kind_results = [r for r in results if r.kind == kind]
        if not kind_results:
            continue

        table = Table(
            title=f"{kind_labels[kind]} ({len(kind_results)})",
            show_header=True,
            header_style="bold",
        )
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("Upstream Ref", style="dim")
        table.add_column("Ahead", style="green", justify="right")
        table.add_column("Behind", style="red", justify="right")
        table.add_column("Status", style="dim")

        for r in kind_results:
            if r.error:
                status = f"[red]{r.error}[/red]"
            elif r.ahead == 0 and r.behind == 0:
                status = "[green]in sync[/green]"
            else:
                parts = []
                if r.ahead:
                    parts.append(f"[green]+{r.ahead}[/green]")
                if r.behind:
                    parts.append(f"[red]-{r.behind}[/red]")
                status = " ".join(parts)

            table.add_row(
                truncate_project_name(r.project_path),
                r.upstream_ref,
                str(r.ahead) if r.ahead else "",
                str(r.behind) if r.behind else "",
                status,
            )

        console.print(table)
        console.print()

    # Summary
    total_ahead = sum(r.ahead for r in results)
    total_behind = sum(r.behind for r in results)
    errors = [r for r in results if r.error]
    console.print(f"[bold]Summary:[/bold] {len(results)} projects with differences, "
                  f"[green]+{total_ahead}[/green] ahead, [red]-{total_behind}[/red] behind"
                  + (f", [red]{len(errors)} errors[/red]" if errors else ""))

    # Detailed commit listings per repo
    detailed = [r for r in results if r.ahead_commits or r.behind_commits]
    if detailed:
        console.print("\n" + "=" * 60)
        console.print("[bold]Commit details[/bold]\n")

        def print_commits(commits: List[str], prefix: str, style: str):
            if len(commits) <= 10:
                for line in commits:
                    console.print(f"    [{style}]{prefix}[/{style}] {line}")
            else:
                for line in commits[:5]:
                    console.print(f"    [{style}]{prefix}[/{style}] {line}")
                console.print(f"    [dim]... {len(commits) - 10} more ...[/dim]")
                for line in commits[-5:]:
                    console.print(f"    [{style}]{prefix}[/{style}] {line}")

        for r in detailed:
            console.print(f"[bold cyan]{r.project_path}[/bold cyan]")
            if r.ahead_commits:
                console.print(f"  [green]Local ahead ({len(r.ahead_commits)}):[/green]")
                print_commits(r.ahead_commits, "+", "green")
            if r.behind_commits:
                console.print(f"  [red]Remote ahead ({len(r.behind_commits)}):[/red]")
                print_commits(r.behind_commits, "-", "red")
            console.print()

    # Uncommitted changes
    dirty = [r for r in results if r.dirty_stat]
    if dirty:
        console.print("=" * 60)
        console.print("[bold]Uncommitted changes[/bold]\n")

        for r in dirty:
            console.print(f"[bold cyan]{r.project_path}[/bold cyan]")
            console.print(r.dirty_stat)
            console.print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
