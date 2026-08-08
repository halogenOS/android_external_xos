"""Live progress state and rendering for repository mirror operations."""

import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from rich.progress import Progress
from rich.table import Table


@dataclass
class MirrorActivity:
    """One repository's current preparation or ref push."""

    project_name: str
    phase: str
    kind: Optional[str] = None
    item: Optional[str] = None
    index: int = 0
    count: int = 0
    note: Optional[str] = None


class CleanMirrorStats:
    """Thread-safe counters and in-flight clean mirror work."""

    def __init__(self, repos_total: int):
        self._lock = threading.Lock()
        self._next_id = 0
        self.inflight: Dict[int, Tuple[MirrorActivity, float]] = {}
        self.repos_total = repos_total
        self.repos_done = 0
        self.repos_failed = 0
        self.branches_queued = 0
        self.tags_queued = 0
        self.refs_done = 0
        self.pushed = 0
        self.failed = 0
        self.skipped = 0
        self.current = 0
        self.local_repositories = 0
        self.cached_repositories = 0
        self.created_repositories = 0

    def start_repository(self, project_name: str) -> int:
        with self._lock:
            self._next_id += 1
            activity = MirrorActivity(project_name, "preparing")
            self.inflight[self._next_id] = (activity, time.monotonic())
            return self._next_id

    def update_activity(self, activity_id: int, restart: bool = False, **changes):
        with self._lock:
            activity, started = self.inflight[activity_id]
            for name, value in changes.items():
                setattr(activity, name, value)
            if restart:
                started = time.monotonic()
            self.inflight[activity_id] = (activity, started)

    def record_queue(self, branches: int, tags: int):
        with self._lock:
            self.branches_queued += branches
            self.tags_queued += tags

    def record_ref(self, outcome: str):
        with self._lock:
            self.refs_done += 1
            if outcome == "pushed":
                self.pushed += 1
            elif outcome == "failed":
                self.failed += 1
            elif outcome == "skipped":
                self.skipped += 1
            elif outcome == "current":
                self.current += 1

    def finish_repository(
        self,
        activity_id: int,
        failed: bool,
        used_cache: bool,
        created_repository: bool,
    ):
        with self._lock:
            self.inflight.pop(activity_id, None)
            self.repos_done += 1
            if failed:
                self.repos_failed += 1
            if used_cache:
                self.cached_repositories += 1
            else:
                self.local_repositories += 1
            if created_repository:
                self.created_repositories += 1

    def total_refs(self) -> int:
        with self._lock:
            return self.branches_queued + self.tags_queued

    def snapshot(self) -> Tuple[dict, List[Tuple[MirrorActivity, float]]]:
        with self._lock:
            counters = {
                name: value
                for name, value in self.__dict__.items()
                if not name.startswith("_") and name != "inflight"
            }
            inflight = [
                (replace(activity), started)
                for activity, started in self.inflight.values()
            ]
        return counters, sorted(inflight, key=lambda entry: entry[1])


class CleanMirrorTracker:
    """Connect one repository worker to the shared live status."""

    def __init__(self, stats: CleanMirrorStats, progress: Progress, ref_task, project_name: str):
        self.stats = stats
        self.progress = progress
        self.ref_task = ref_task
        self.activity_id = stats.start_repository(project_name)

    def status(self, phase: str, note: Optional[str] = None, restart: bool = True):
        self.stats.update_activity(
            self.activity_id,
            phase=phase,
            kind=None,
            item=None,
            index=0,
            count=0,
            note=note,
            restart=restart,
        )

    def queue(self, branches: int, tags: int):
        self.stats.record_queue(branches, tags)
        self.progress.update(self.ref_task, total=self.stats.total_refs())

    def start_ref(self, kind: str, item: str, index: int, count: int):
        self.stats.update_activity(
            self.activity_id,
            phase="pushing",
            kind=kind,
            item=item,
            index=index,
            count=count,
            note=None,
            restart=True,
        )

    def note(self, note: Optional[str]):
        self.stats.update_activity(self.activity_id, note=note)

    def finish_ref(self, outcome: str):
        self.stats.record_ref(outcome)
        self.progress.update(self.ref_task, advance=1)


def format_duration(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def render_clean_status(stats: CleanMirrorStats, max_rows: int = 8) -> Table:
    """Render exact counters and every repository currently doing work."""
    counters, inflight = stats.snapshot()
    queued = counters["branches_queued"] + counters["tags_queued"]

    grid = Table.grid(padding=(0, 1))
    grid.add_column()
    grid.add_row(
        f"[bold]Refs[/bold] {counters['refs_done']}/{queued} · "
        f"[green]{counters['pushed']} pushed[/green] · "
        f"[yellow]{counters['skipped']} skipped[/yellow] · "
        f"[red]{counters['failed']} failed[/red] · "
        f"[dim]{counters['current']} already current[/dim]"
    )
    grid.add_row(
        f"[bold]Repos[/bold] {counters['repos_done']}/{counters['repos_total']} · "
        f"[red]{counters['repos_failed']} failed[/red] · "
        f"[dim]{counters['local_repositories']} local, "
        f"{counters['cached_repositories']} cached, "
        f"{counters['created_repositories']} created[/dim]"
    )

    if inflight:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        table.add_column(style="dim", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)

        now = time.monotonic()
        for activity, started in inflight[:max_rows]:
            position = f"{activity.index}/{activity.count}" if activity.count else ""
            detail = activity.phase
            if activity.item:
                detail = f"{activity.kind} {activity.item}"
            if activity.note:
                detail += f" [yellow]— {activity.note}[/yellow]"
            table.add_row(
                "  ↑",
                activity.project_name,
                position,
                detail,
                format_duration(now - started),
            )

        if len(inflight) > max_rows:
            table.add_row("", f"[dim]… and {len(inflight) - max_rows} more[/dim]", "", "", "")
        grid.add_row(table)

    return grid
