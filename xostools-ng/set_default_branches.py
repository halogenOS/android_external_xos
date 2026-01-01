#!/usr/bin/env python3
"""
Set default branches for all repositories in a GitLab group.
Selects the latest available XOS-* branch as the default.
Also ensures all repositories are public.
"""

import sys
import signal
from packaging import version
import gitlab
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
from typing import Optional, List, Callable, Dict
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn, TaskID
from rich.table import Table
from xos_common import get_gitlab_token, console, GITLAB_URL, GITLAB_GROUP_ID, truncate_project_name

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
    project_id: int
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


def select_latest_xos_branch(branches):
    """Select the latest XOS branch from a list of branch names."""
    xos_branches = []

    for branch in branches:
        parsed_version = parse_xos_version(branch.name)
        if parsed_version is not None:
            xos_branches.append((branch.name, parsed_version))

    if not xos_branches:
        return None

    xos_branches.sort(key=lambda x: x[1], reverse=True)
    return xos_branches[0][0]


def process_project(
    gl: gitlab.Gitlab,
    project_id: int,
    project_name: str,
    status_callback: Optional[StatusCallback] = None
) -> ProcessResult:
    """Process a single project and update its default branch if needed."""

    def update_status(status: str, persistent: bool = False, advance: int = 0, add_steps: int = 0):
        if status_callback:
            status_callback(status, persistent, advance, add_steps)

    try:
        # Get full project details
        update_status("Fetching project details", add_steps=1)
        full_project = gl.projects.get(project_id)
        current_default = full_project.default_branch
        update_status("Got details", advance=1)

        # Get all branches for this project
        update_status("Listing branches", add_steps=1)
        branches = list(full_project.branches.list(iterator=True))
        update_status("Listed branches", advance=1)

        if not branches:
            update_status("[dim]Skipped - no branches[/dim]", persistent=True)
            return ProcessResult(
                project_id=project_id,
                project_name=project_name,
                status='skipped',
                message='No branches found'
            )

        # Select the latest XOS branch
        selected_branch = select_latest_xos_branch(branches)

        if not selected_branch:
            update_status("[dim]Skipped - no XOS branches[/dim]", persistent=True)
            return ProcessResult(
                project_id=project_id,
                project_name=project_name,
                status='skipped',
                message='No XOS branches found'
            )

        # Check if we need to update
        needs_update = False
        changes = []

        if current_default != selected_branch:
            needs_update = True
            changes.append(f'branch: {current_default} → {selected_branch}')

        if full_project.visibility != 'public':
            needs_update = True
            changes.append(f'visibility: {full_project.visibility} → public')

        if not needs_update:
            update_status("[dim]Skipped - already correct[/dim]", persistent=True)
            return ProcessResult(
                project_id=project_id,
                project_name=project_name,
                status='skipped',
                message='Already set correctly',
                old_branch=current_default,
                new_branch=selected_branch
            )

        # Update the default branch and visibility
        update_status("Saving changes", add_steps=1)
        full_project.default_branch = selected_branch
        full_project.visibility = 'public'
        full_project.save()
        update_status("Saved", advance=1)

        update_status(f"[green]Updated: {', '.join(changes)}[/green]", persistent=True)

        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status='updated',
            message=', '.join(changes),
            old_branch=current_default,
            new_branch=selected_branch
        )

    except Exception as e:
        update_status(f"[red]Error: {e}[/red]", persistent=True)
        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status='error',
            message=str(e)
        )


def main():
    """Main function to process all repositories."""
    global executor

    # Initialize GitLab connection
    token = get_gitlab_token()
    if not token:
        console.print("[red]Error: GitLab token not found. Make sure ~/.creds/xos_gitlab_token exists.[/red]")
        sys.exit(1)

    console.print(f"[cyan]Connecting to GitLab at {GITLAB_URL}...[/cyan]")
    gl = gitlab.Gitlab(GITLAB_URL, private_token=token)

    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError:
        console.print("[red]Error: Failed to authenticate with GitLab. Check your token.[/red]")
        sys.exit(1)

    console.print(f"[green]✓[/green] Connected to GitLab")

    # Get the group
    console.print(f"[cyan]Fetching group {GITLAB_GROUP_ID}...[/cyan]")
    try:
        group = gl.groups.get(GITLAB_GROUP_ID)
    except gitlab.exceptions.GitlabGetError:
        console.print(f"[red]Error: Could not find group with ID {GITLAB_GROUP_ID}[/red]")
        sys.exit(1)

    console.print(f"[green]✓[/green] Group: [bold]{group.name}[/bold]")

    # Fetch project list
    console.print(f"[cyan]Fetching project list...[/cyan]")
    projects = list(group.projects.list(include_subgroups=False, iterator=True))
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
            with worker_slots_lock:
                if available_slots:
                    slot_id = available_slots.pop(0)
                else:
                    slot_id = 0

            task_id = worker_task_ids[slot_id]
            project_display = truncate_project_name(project.path_with_namespace, max_len=40)
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

            status_callback = create_status_callback(project.path_with_namespace, slot_id)

            try:
                result = process_project(
                    gl,
                    project.id,
                    project.path_with_namespace,
                    status_callback
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
                        project_id=project.id,
                        project_name=project.path_with_namespace,
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
