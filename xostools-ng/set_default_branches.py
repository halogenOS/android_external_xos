#!/usr/bin/env python3
"""
Set default branches for all repositories in a GitLab group.
Selects the latest available XOS-* branch as the default.
"""

import os
import sys
from pathlib import Path
from packaging import version
import gitlab
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
from typing import Optional, List, Tuple
from xos_common import get_gitlab_token, console, GITLAB_URL, GITLAB_GROUP_ID

# Configuration
MAX_WORKERS = 10  # Number of concurrent threads

# Thread-safe counters
counter_lock = threading.Lock()
stats = {
    'updated': 0,
    'skipped': 0,
    'errors': 0
}

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

    # Extract version part (e.g., "16.0" from "XOS-16.0")
    version_str = branch_name[4:]  # Remove "XOS-" prefix

    try:
        # Use packaging.version for proper version comparison
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

    # Sort by version in descending order (latest first)
    xos_branches.sort(key=lambda x: x[1], reverse=True)

    return xos_branches[0][0]  # Return the branch name

def process_project(gl: gitlab.Gitlab, project_id: int, project_name: str, pbar: tqdm) -> ProcessResult:
    """Process a single project and update its default branch if needed."""
    try:
        # Get full project details
        full_project = gl.projects.get(project_id)
        current_default = full_project.default_branch

        # Get all branches for this project
        branches = list(full_project.branches.list(iterator=True))

        if not branches:
            with counter_lock:
                stats['skipped'] += 1
            pbar.update(1)
            return ProcessResult(
                project_id=project_id,
                project_name=project_name,
                status='skipped',
                message='No branches found'
            )

        # Select the latest XOS branch
        selected_branch = select_latest_xos_branch(branches)

        if not selected_branch:
            with counter_lock:
                stats['skipped'] += 1
            pbar.update(1)
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
            with counter_lock:
                stats['skipped'] += 1
            pbar.update(1)
            return ProcessResult(
                project_id=project_id,
                project_name=project_name,
                status='skipped',
                message='Already set correctly',
                old_branch=current_default,
                new_branch=selected_branch
            )

        # Update the default branch and visibility
        full_project.default_branch = selected_branch
        full_project.visibility = 'public'
        full_project.save()

        with counter_lock:
            stats['updated'] += 1
        pbar.update(1)

        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status='updated',
            message=', '.join(changes),
            old_branch=current_default,
            new_branch=selected_branch
        )

    except Exception as e:
        with counter_lock:
            stats['errors'] += 1
        pbar.update(1)
        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status='error',
            message=str(e)
        )

def main():
    """Main function to process all repositories."""
    # Initialize GitLab connection
    token = get_gitlab_token()
    if not token:
        console.print("[red]Error: GitLab token not found. Make sure ~/.creds/xos_gitlab_token exists.[/red]")
        sys.exit(1)

    gl = gitlab.Gitlab(GITLAB_URL, private_token=token)

    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError:
        console.print("[red]Error: Failed to authenticate with GitLab. Check your token.[/red]")
        sys.exit(1)

    console.print(f"Connected to GitLab at {GITLAB_URL}")

    # Get the group
    try:
        group = gl.groups.get(GITLAB_GROUP_ID)
    except gitlab.exceptions.GitlabGetError:
        console.print(f"[red]Error: Could not find group with ID {GITLAB_GROUP_ID}[/red]")
        sys.exit(1)

    console.print(f"Processing repositories in group: {group.name} (ID: {GITLAB_GROUP_ID})")
    console.print(f"Using {MAX_WORKERS} concurrent workers")
    console.print()

    # First, collect all projects (we need the total count for the progress bar)
    console.print("Fetching project list...")
    projects = list(tqdm(
        group.projects.list(include_subgroups=False, iterator=True),
        desc="Discovering projects",
        unit="projects"
    ))

    total_projects = len(projects)
    console.print(f"\nFound {total_projects} projects to process")
    console.print()

    # Results storage
    results: List[ProcessResult] = []

    # Process projects with thread pool
    with tqdm(total=total_projects, desc="Processing projects", unit="projects") as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all tasks
            future_to_project = {
                executor.submit(
                    process_project,
                    gl,
                    project.id,
                    project.path_with_namespace,
                    pbar
                ): project
                for project in projects
            }

            # Collect results as they complete
            for future in as_completed(future_to_project):
                result = future.result()
                results.append(result)

    # Display summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Total projects processed: {total_projects}")
    print(f"  ✓ Updated: {stats['updated']}")
    print(f"  - Skipped: {stats['skipped']}")
    print(f"  ✗ Errors: {stats['errors']}")

    # Show updated projects
    if stats['updated'] > 0:
        print(f"\n{'='*60}")
        print("UPDATED PROJECTS")
        print(f"{'='*60}")
        for result in results:
            if result.status == 'updated':
                print(f"  {result.project_name}: {result.old_branch} → {result.new_branch}")

    # Show errors if any
    if stats['errors'] > 0:
        print(f"\n{'='*60}")
        print("ERRORS")
        print(f"{'='*60}")
        for result in results:
            if result.status == 'error':
                print(f"  {result.project_name}: {result.message}")

if __name__ == "__main__":
    main()
