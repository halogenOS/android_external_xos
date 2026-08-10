#!/usr/bin/env python3
"""
Manage Forgejo webhooks for all repositories in an organization.
Ensures each repository has exactly one push webhook with the specified URL.
"""

import sys
import argparse
from urllib.parse import urlparse
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
from typing import Dict, List
from xos_common import (
    FORGEJO_OWNER,
    FORGEJO_URL,
    ForgejoClient,
    console,
    get_forgejo_token,
    hook_target_url,
    repository_display_name,
)

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
    project_name: str
    status: str  # 'added', 'removed', 'skipped', 'error'
    message: str
    webhooks_removed: int = 0
    webhook_added: bool = False


def is_push_webhook(webhook_url: str) -> bool:
    """Check if a webhook URL ends with /push."""
    return bool(webhook_url) and webhook_url.rstrip('/').endswith('/push')


def delivers_push_events(webhook: Dict) -> bool:
    """Whether a hook would actually deliver the pushes we need.

    A disabled hook, or one subscribed to other events, points at the right URL
    without ever firing, so it cannot stand in for the hook this tool
    guarantees. Fields the server does not report are not held against a hook,
    so an unexpected payload shape never causes delete-and-recreate churn.
    """
    if not webhook.get('active', True):
        return False
    events = webhook.get('events')
    return events is None or 'push' in events

def process_project_webhooks(client: ForgejoClient, owner: str, repository_name: str,
                             project_name: str, target_url: str, pbar: tqdm) -> ProcessResult:
    """Process webhooks for a single repository."""
    try:
        # Get all webhooks for this repository
        webhooks = client.list_hooks(repository_name, owner=owner)

        # Track actions taken
        webhooks_removed = 0
        webhook_exists = False
        webhook_added = False

        # Check existing webhooks. Exactly one hook may deliver our pushes, so
        # keep the first usable one and remove every other push hook: a stale
        # URL, a disabled or non-push hook on our URL, and duplicates of our
        # own hook left behind by a concurrent run.
        for webhook in webhooks:
            existing_url = hook_target_url(webhook)
            if not is_push_webhook(existing_url):
                continue

            if (existing_url == target_url and delivers_push_events(webhook)
                    and not webhook_exists):
                webhook_exists = True
                continue

            try:
                client.delete_hook(repository_name, webhook['id'], owner=owner)
                webhooks_removed += 1
            except Exception as e:
                pbar.update(1)
                with counter_lock:
                    stats['errors'] += 1
                return ProcessResult(
                    project_name=project_name,
                    status='error',
                    message=f'Failed to remove webhook: {e}'
                )

        # Add webhook if needed
        if not webhook_exists:
            try:
                # Create new webhook with push events for all branches
                client.create_hook(
                    repository_name,
                    target_url,
                    owner=owner,
                    events=['push'],
                    active=True,
                )
                webhook_added = True
            except Exception as e:
                pbar.update(1)
                with counter_lock:
                    stats['errors'] += 1
                return ProcessResult(
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
        description='Manage Forgejo webhooks for all repositories in an organization'
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

    # Initialize Forgejo connection
    token = get_forgejo_token()
    if not token:
        console.print(
            "[red]Error: Forgejo token not found. Set XOS_FORGEJO_TOKEN or "
            "create ~/.creds/xos_forgejo_token.[/red]"
        )
        sys.exit(1)

    client = ForgejoClient(token=token)

    try:
        client.authenticated_user()
    except Exception as error:
        console.print(f"[red]Error: Failed to authenticate with Forgejo: {error}[/red]")
        sys.exit(1)

    console.print(f"Connected to Forgejo at {FORGEJO_URL}")

    # Get the organization
    try:
        organization = client.get_organization(FORGEJO_OWNER)
    except Exception as error:
        console.print(f"[red]Error: Could not find organization {FORGEJO_OWNER}: {error}[/red]")
        sys.exit(1)

    organization_name = organization.get('full_name') or organization.get('username') or FORGEJO_OWNER
    console.print(f"Processing repositories in organization: {organization_name} ({FORGEJO_OWNER})")
    console.print(f"Target webhook URL: {args.webhook_url}")
    if args.dry_run:
        console.print("[blue]DRY RUN MODE - No changes will be made[/blue]")
    console.print(f"Using {MAX_WORKERS} concurrent workers")
    console.print()

    # First, collect all repositories
    console.print("Fetching repository list...")
    try:
        projects = client.list_organization_repositories(FORGEJO_OWNER)
    except Exception as error:
        console.print(f"[red]Error: Could not list repositories: {error}[/red]")
        sys.exit(1)

    total_projects = len(projects)
    console.print(f"\nFound {total_projects} projects to process")
    console.print()

    if args.dry_run:
        console.print("Would process the following projects:")
        for project in projects[:10]:  # Show first 10 as example
            console.print(f"  - {repository_display_name(project)}")
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
                    client,
                    (project.get("owner") or {}).get("login") or FORGEJO_OWNER,
                    project.get("name"),
                    repository_display_name(project),
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
