#!/usr/bin/env python3
"""
Set default branches for all repositories in a Forgejo organization.
Selects the latest available XOS-* branch as the default.
Also ensures all repositories are public.
"""

import sys
import signal
from packaging import version
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
from typing import Optional, List, Callable, Dict, Iterable
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn, TaskID
from rich.table import Table
from xos_common import (
    FORGEJO_OWNER,
    FORGEJO_URL,
    ForgejoClient,
    console,
    get_forgejo_token,
    repository_display_name,
    repository_is_empty,
    truncate_project_name,
)

# Configuration
MAX_WORKERS = 10

# Global stop event for graceful shutdown
stop_event = threading.Event()
executor = None

# Type alias for status callback
StatusCallback = Callable[[str, bool, int, int], None]


def signal_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) gracefully."""
    console.print("\n[red]Interrupted! Shutting down workers...[/red]")
    stop_event.set()
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)
    sys.exit(1)


signal.signal(signal.SIGINT, signal_handler)


@dataclass
class ProcessResult:
    project_name: str
    status: str  # 'updated', 'skipped', 'error'
    message: str
    old_branch: Optional[str] = None
    new_branch: Optional[str] = None


def parse_xos_version(branch_name):
    """Parse XOS version from branch name for sorting."""
    if not branch_name.startswith("XOS-"):
        return None

    version_str = branch_name[4:]  # Remove "XOS-" prefix

    try:
        return version.parse(version_str)
    except:
        return None


def branch_names(branches: Iterable) -> List[str]:
    """Read the names out of the Forgejo branch objects."""
    names = []
    for branch in branches:
        name = branch.get("name") if isinstance(branch, dict) else getattr(branch, "name", None)
        if name:
            names.append(name)
    return names


def select_latest_xos_branch(names: Iterable[str]) -> Optional[str]:
    """Select the latest XOS branch from a list of branch names."""
    xos_branches = []

    for name in names:
        parsed_version = parse_xos_version(name)
        if parsed_version is not None:
            xos_branches.append((name, parsed_version))

    if not xos_branches:
        return None

    xos_branches.sort(key=lambda x: x[1], reverse=True)
    return xos_branches[0][0]


def process_project(
    client: ForgejoClient,
    owner: str,
    repository_name: str,
    project_name: str,
    status_callback: Optional[StatusCallback] = None,
    cancel_event: Optional[threading.Event] = None
) -> ProcessResult:
    """Process a single repository and update its default branch if needed."""

    def update_status(status: str, persistent: bool = False, advance: int = 0, add_steps: int = 0):
        if status_callback:
            status_callback(status, persistent, advance, add_steps)

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def cancellation_result() -> ProcessResult:
        update_status("[yellow]Cancelled[/yellow]", persistent=True)
        return ProcessResult(
            project_name=project_name,
            status='skipped',
            message='Cancelled'
        )

    try:
        # An interrupt has to stop this worker before it changes anything, so
        # the cancellation is checked again right before the write below.
        if cancelled():
            return cancellation_result()

        # Get full repository details
        update_status("Fetching repository details", add_steps=1)
        repository = client.get_repository(repository_name, owner=owner)
        current_default = repository.get("default_branch")
        is_private = bool(repository.get("private"))
        update_status("Got details", advance=1)

        # A repository without commits has no ref endpoints at all, so asking
        # for its branches would fail the whole repository and leave it private.
        if repository_is_empty(repository):
            branches = []
        else:
            # Get all branches for this repository
            update_status("Listing branches", add_steps=1)
            branches = client.list_branches(repository_name, owner=owner)
            update_status("Listed branches", advance=1)

        # Visibility is enforced independently of the default branch: a freshly
        # created repo (no branches, or no XOS branch yet) must still be made
        # public, since a raw push creates repositories private.
        selected_branch = select_latest_xos_branch(branch_names(branches)) if branches else None

        needs_update = False
        changes = []

        if selected_branch and current_default != selected_branch:
            needs_update = True
            changes.append(f'branch: {current_default} → {selected_branch}')

        if is_private:
            needs_update = True
            changes.append('visibility: private → public')

        if not needs_update:
            if not selected_branch:
                update_status("[dim]Skipped - no XOS branch, already public[/dim]",
                              persistent=True)
            else:
                update_status("[dim]Skipped - already correct[/dim]", persistent=True)
            return ProcessResult(
                project_name=project_name,
                status='skipped',
                message='Already set correctly',
                old_branch=current_default,
                new_branch=selected_branch
            )

        if cancelled():
            return cancellation_result()

        # Apply the changes. Only touch default_branch when we actually have a
        # branch to point at; visibility is always corrected when it's wrong.
        update_status("Saving changes", add_steps=1)
        fields = {'private': False}
        if selected_branch:
            fields['default_branch'] = selected_branch
        client.edit_repository(repository_name, owner=owner, **fields)
        update_status("Saved", advance=1)

        update_status(f"[green]Updated: {', '.join(changes)}[/green]", persistent=True)

        return ProcessResult(
            project_name=project_name,
            status='updated',
            message=', '.join(changes),
            old_branch=current_default,
            new_branch=selected_branch
        )

    except Exception as e:
        update_status(f"[red]Error: {e}[/red]", persistent=True)
        return ProcessResult(
            project_name=project_name,
            status='error',
            message=str(e)
        )


def main():
    """Main function to process all repositories."""
    global executor

    # Initialize Forgejo connection
    token = get_forgejo_token()
    if not token:
        console.print(
            "[red]Error: Forgejo token not found. Set XOS_FORGEJO_TOKEN or "
            "create ~/.creds/xos_forgejo_token.[/red]"
        )
        sys.exit(1)

    console.print(f"[cyan]Connecting to Forgejo at {FORGEJO_URL}...[/cyan]")
    client = ForgejoClient(token=token)

    try:
        client.authenticated_user()
    except Exception as error:
        console.print(f"[red]Error: Failed to authenticate with Forgejo: {error}[/red]")
        sys.exit(1)

    console.print("[green]✓[/green] Connected to Forgejo")

    # Get the organization
    console.print(f"[cyan]Fetching organization {FORGEJO_OWNER}...[/cyan]")
    try:
        organization = client.get_organization(FORGEJO_OWNER)
    except Exception as error:
        console.print(f"[red]Error: Could not find organization {FORGEJO_OWNER}: {error}[/red]")
        sys.exit(1)

    console.print(
        f"[green]✓[/green] Organization: "
        f"[bold]{organization.get('full_name') or organization.get('username') or FORGEJO_OWNER}[/bold]"
    )

    # Fetch repository list
    console.print("[cyan]Fetching repository list...[/cyan]")
    try:
        projects = client.list_organization_repositories(FORGEJO_OWNER)
    except Exception as error:
        console.print(f"[red]Error: Could not list repositories: {error}[/red]")
        sys.exit(1)

    total_projects = len(projects)
    console.print(f"[green]✓[/green] Found [bold]{total_projects}[/bold] projects")
    console.print(f"[dim]Using {MAX_WORKERS} concurrent workers[/dim]\n")

    # Results storage
    results: List[ProcessResult] = []

    # Worker status management
    worker_slots_lock = threading.Lock()
    worker_task_ids: Dict[int, TaskID] = {}
    available_slots: List[int] = list(range(MAX_WORKERS))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        expand=False
    ) as progress:

        main_task = progress.add_task(
            "[cyan]Setting default branches",
            total=total_projects
        )

        # Create worker status tasks (initially hidden)
        for slot_id in range(MAX_WORKERS):
            task_id = progress.add_task(
                f"[dim]  Worker {slot_id}: idle[/dim]",
                total=0,
                completed=0,
                visible=False
            )
            worker_task_ids[slot_id] = task_id

        def create_status_callback(project_name: str, slot_id: int) -> StatusCallback:
            """Create a callback that updates the worker's progress task."""
            project_display = truncate_project_name(project_name, max_len=40)

            def callback(status: str, persistent: bool = False, advance: int = 0, add_steps: int = 0):
                task_id = worker_task_ids[slot_id]

                if add_steps > 0:
                    current_total = progress.tasks[task_id].total or 0
                    progress.update(task_id, total=current_total + add_steps)

                if advance > 0:
                    progress.update(task_id, advance=advance)

                if persistent:
                    progress.console.print(f"  [cyan]{project_display}:[/cyan] {status}")
                else:
                    progress.update(
                        task_id,
                        description=f"  [cyan]{project_display}:[/cyan] {status}"
                    )

            return callback

        def run_task_with_status(project) -> ProcessResult:
            """Wrapper that assigns a slot, runs the task, and releases the slot."""
            if stop_event.is_set():
                return ProcessResult(
                    project_name=repository_display_name(project),
                    status='skipped',
                    message='Cancelled'
                )

            with worker_slots_lock:
                if available_slots:
                    slot_id = available_slots.pop(0)
                else:
                    slot_id = 0

            task_id = worker_task_ids[slot_id]
            project_name = repository_display_name(project)
            project_display = truncate_project_name(project_name, max_len=40)
            # Reset task start time for accurate per-task elapsed time
            progress.tasks[task_id].start_time = progress.get_time()
            progress.tasks[task_id].stop_time = None
            progress.update(
                task_id,
                description=f"  [cyan]{project_display}:[/cyan] Starting...",
                completed=0,
                total=0,
                visible=True
            )

            status_callback = create_status_callback(project_name, slot_id)

            try:
                result = process_project(
                    client,
                    (project.get("owner") or {}).get("login") or FORGEJO_OWNER,
                    project.get("name"),
                    project_name,
                    status_callback,
                    stop_event
                )
                return result
            finally:
                progress.update(
                    task_id,
                    description=f"[dim]  Worker {slot_id}: idle[/dim]",
                    visible=False
                )
                with worker_slots_lock:
                    available_slots.append(slot_id)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor_pool:
            executor = executor_pool

            futures = {
                executor_pool.submit(run_task_with_status, project): project
                for project in projects
            }

            for future in as_completed(futures):
                if stop_event.is_set():
                    executor_pool.shutdown(wait=False, cancel_futures=True)
                    break

                try:
                    result = future.result()
                    results.append(result)
                    progress.update(main_task, advance=1)
                except Exception as e:
                    project = futures[future]
                    results.append(ProcessResult(
                        project_name=repository_display_name(project),
                        status='error',
                        message=str(e)
                    ))
                    progress.update(main_task, advance=1)

    # Calculate stats
    updated = len([r for r in results if r.status == 'updated'])
    skipped = len([r for r in results if r.status == 'skipped'])
    errors = len([r for r in results if r.status == 'error'])

    # Display summary
    console.print("\n" + "=" * 60)
    console.print("[bold]SUMMARY[/bold]")
    console.print("=" * 60)
    console.print(f"Total projects processed: {total_projects}")
    console.print(f"  [green]✓ Updated:[/green] {updated}")
    console.print(f"  [dim]- Skipped:[/dim] {skipped}")
    console.print(f"  [red]✗ Errors:[/red] {errors}")

    # Show updated projects
    if updated > 0:
        console.print(f"\n[bold green]Updated projects ({updated}):[/bold green]")
        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Project", style="cyan")
        table.add_column("Changes", style="green")

        for result in results:
            if result.status == 'updated':
                table.add_row(
                    truncate_project_name(result.project_name),
                    result.message
                )
        console.print(table)

    # Show errors if any
    if errors > 0:
        console.print(f"\n[bold red]Errors ({errors}):[/bold red]")
        error_table = Table(show_header=True, header_style="bold red")
        error_table.add_column("Project", style="cyan")
        error_table.add_column("Error", style="red")

        for result in results:
            if result.status == 'error':
                error_table.add_row(
                    truncate_project_name(result.project_name),
                    result.message
                )
        console.print(error_table)


if __name__ == "__main__":
    main()
