#!/usr/bin/env python3
"""
Manage GitLab webhooks for all repositories in a group.
Ensures each repository has exactly one push webhook with the specified URL.
"""

import sys
import argparse
from pathlib import Path
from urllib.parse import urlparse
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
    'added': 0,
    'removed': 0,
    'skipped': 0,
    'errors': 0
}

@dataclass
class ProcessResult:
    project_id: int
    project_name: str
    status: str  # 'added', 'removed', 'skipped', 'error'
    message: str
    webhooks_removed: int = 0
    webhook_added: bool = False


def is_push_webhook(webhook_url: str) -> bool:
    """Check if a webhook URL ends with /push."""
    return webhook_url.rstrip('/').endswith('/push')

def process_project_webhooks(gl: gitlab.Gitlab, project_id: int, project_name: str,
                           target_url: str, pbar: tqdm) -> ProcessResult:
    """Process webhooks for a single project."""
    try:
        # Get full project details
        project = gl.projects.get(project_id)

        # Get all webhooks for this project
        webhooks = list(project.hooks.list(iterator=True))

        # Track actions taken
        webhooks_removed = 0
        webhook_exists = False
        webhook_added = False

        # Check existing webhooks
        for webhook in webhooks:
            if is_push_webhook(webhook.url):
                if webhook.url == target_url:
                    # Correct webhook already exists
                    webhook_exists = True
                else:
                    # Wrong push webhook, remove it
                    try:
                        webhook.delete()
                        webhooks_removed += 1
                    except Exception as e:
                        pbar.update(1)
                        with counter_lock:
                            stats['errors'] += 1
                        return ProcessResult(
                            project_id=project_id,
                            project_name=project_name,
                            status='error',
                            message=f'Failed to remove webhook: {e}'
                        )

        # Add webhook if needed
        if not webhook_exists:
            try:
                # Create new webhook with push events for all branches
                project.hooks.create({
                    'url': target_url,
                    'push_events': True,
                    'push_events_branch_filter': '',  # Empty string means all branches
                    'enable_ssl_verification': True,
                    'token': '',  # No secret token
                    'issues_events': False,
                    'merge_requests_events': False,
                    'wiki_page_events': False,
                    'tag_push_events': False,
                    'note_events': False,
                    'job_events': False,
                    'pipeline_events': False,
                    'deployment_events': False,
                    'releases_events': False
                })
                webhook_added = True
            except Exception as e:
                pbar.update(1)
                with counter_lock:
                    stats['errors'] += 1
                return ProcessResult(
                    project_id=project_id,
                    project_name=project_name,
                    status='error',
                    message=f'Failed to add webhook: {e}',
                    webhooks_removed=webhooks_removed
                )

        # Update statistics and determine status
        if webhook_added:
            with counter_lock:
                stats['added'] += 1
                if webhooks_removed > 0:
                    stats['removed'] += webhooks_removed
            status = 'added'
            message = f'Added webhook and removed {webhooks_removed} old webhooks' if webhooks_removed > 0 else 'Added webhook'
        elif webhooks_removed > 0:
            with counter_lock:
                stats['removed'] += webhooks_removed
            status = 'removed'
            message = f'Removed {webhooks_removed} incorrect webhook(s)'
        else:
            with counter_lock:
                stats['skipped'] += 1
            status = 'skipped'
            message = 'Webhook already configured correctly'

        pbar.update(1)
        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status=status,
            message=message,
            webhooks_removed=webhooks_removed,
            webhook_added=webhook_added
        )

    except Exception as e:
        pbar.update(1)
        with counter_lock:
            stats['errors'] += 1
        return ProcessResult(
            project_id=project_id,
            project_name=project_name,
            status='error',
            message=str(e)
        )

def validate_url(url: str) -> bool:
    """Validate that the URL is properly formatted."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def main():
    """Main function to process all repositories."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Manage GitLab webhooks for all repositories in a group'
    )
    parser.add_argument(
        'webhook_url',
        help='The webhook URL to set (must end with /push)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    # Validate webhook URL
    if not args.webhook_url.rstrip('/').endswith('/push'):
        print("Error: Webhook URL must end with '/push'")
        sys.exit(1)

    if not validate_url(args.webhook_url):
        print("Error: Invalid URL format")
        sys.exit(1)

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
    console.print(f"Target webhook URL: {args.webhook_url}")
    if args.dry_run:
        console.print("[blue]DRY RUN MODE - No changes will be made[/blue]")
    console.print(f"Using {MAX_WORKERS} concurrent workers")
    console.print()

    # First, collect all projects
    console.print("Fetching project list...")
    projects = list(tqdm(
        group.projects.list(include_subgroups=False, iterator=True),
        desc="Discovering projects",
        unit="projects"
    ))

    total_projects = len(projects)
    console.print(f"\nFound {total_projects} projects to process")
    console.print()

    if args.dry_run:
        console.print("Would process the following projects:")
        for project in projects[:10]:  # Show first 10 as example
            console.print(f"  - {project.path_with_namespace}")
        if len(projects) > 10:
            console.print(f"  ... and {len(projects) - 10} more")
        console.print("\nExiting dry run mode.")
        return

    # Results storage
    results: List[ProcessResult] = []

    # Process projects with thread pool
    with tqdm(total=total_projects, desc="Processing webhooks", unit="projects") as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all tasks
            future_to_project = {
                executor.submit(
                    process_project_webhooks,
                    gl,
                    project.id,
                    project.path_with_namespace,
                    args.webhook_url,
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
    print(f"  ✓ Webhooks added: {stats['added']}")
    print(f"  ✓ Webhooks removed: {stats['removed']}")
    print(f"  - Skipped (already configured): {stats['skipped']}")
    print(f"  ✗ Errors: {stats['errors']}")

    # Show projects where webhooks were added
    added_results = [r for r in results if r.webhook_added]
    if added_results:
        print(f"\n{'='*60}")
        print("WEBHOOKS ADDED")
        print(f"{'='*60}")
        for result in sorted(added_results, key=lambda x: x.project_name):
            print(f"  {result.project_name}")
            if result.webhooks_removed > 0:
                print(f"    (also removed {result.webhooks_removed} incorrect webhook(s))")

    # Show projects where only webhooks were removed
    removed_only = [r for r in results if r.status == 'removed']
    if removed_only:
        print(f"\n{'='*60}")
        print("INCORRECT WEBHOOKS REMOVED")
        print(f"{'='*60}")
        for result in sorted(removed_only, key=lambda x: x.project_name):
            print(f"  {result.project_name}: removed {result.webhooks_removed} webhook(s)")

    # Show errors if any
    error_results = [r for r in results if r.status == 'error']
    if error_results:
        print(f"\n{'='*60}")
        print("ERRORS")
        print(f"{'='*60}")
        for result in sorted(error_results, key=lambda x: x.project_name):
            print(f"  {result.project_name}: {result.message}")

if __name__ == "__main__":
    main()
