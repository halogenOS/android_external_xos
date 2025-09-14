#!/usr/bin/env python3
"""
Set default branches for all repositories in a GitHub organization.
Selects the latest available XOS-* branch as the default.
"""

import sys
from pathlib import Path
from packaging import version
from github import Github, GithubException
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dataclasses import dataclass
from typing import Optional, List
from xos_common import get_github_token, console, GITHUB_ORG

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
    repo_name: str
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

def process_repository(repo, pbar: tqdm) -> ProcessResult:
    """Process a single repository and update its default branch if needed."""
    try:
        repo_name = repo.full_name
        current_default = repo.default_branch
        
        # Get all branches for this repository
        branches = list(repo.get_branches())
        
        if not branches:
            with counter_lock:
                stats['skipped'] += 1
            pbar.update(1)
            return ProcessResult(
                repo_name=repo_name,
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
                repo_name=repo_name,
                status='skipped',
                message='No XOS branches found'
            )
        
        # Check if we need to update
        if current_default == selected_branch:
            with counter_lock:
                stats['skipped'] += 1
            pbar.update(1)
            return ProcessResult(
                repo_name=repo_name,
                status='skipped',
                message='Already set as default',
                old_branch=current_default,
                new_branch=selected_branch
            )
        
        # Update the default branch
        # First check if the branch exists
        try:
            repo.get_branch(selected_branch)
        except GithubException:
            with counter_lock:
                stats['errors'] += 1
            pbar.update(1)
            return ProcessResult(
                repo_name=repo_name,
                status='error',
                message=f'Branch {selected_branch} not found'
            )
        
        # Update the default branch
        repo.edit(default_branch=selected_branch)
        
        with counter_lock:
            stats['updated'] += 1
        pbar.update(1)
        
        return ProcessResult(
            repo_name=repo_name,
            status='updated',
            message=f'Updated from {current_default} to {selected_branch}',
            old_branch=current_default,
            new_branch=selected_branch
        )
        
    except Exception as e:
        with counter_lock:
            stats['errors'] += 1
        pbar.update(1)
        return ProcessResult(
            repo_name=repo.full_name if hasattr(repo, 'full_name') else 'Unknown',
            status='error',
            message=str(e)
        )

def main():
    """Main function to process all repositories."""
    # Initialize GitHub connection
    token = get_github_token()
    if not token:
        console.print("[red]Error: GitHub token not found. Make sure ~/.creds/xos_github_token exists.[/red]")
        sys.exit(1)
    
    g = Github(token)
    
    try:
        # Test authentication by getting the authenticated user
        user = g.get_user()
        console.print(f"Authenticated as: {user.login}")
    except Exception as e:
        console.print(f"[red]Error: Failed to authenticate with GitHub. Check your token. {e}[/red]")
        sys.exit(1)
    
    # Get the organization
    try:
        org = g.get_organization(GITHUB_ORG)
        console.print(f"Processing repositories in organization: {org.name} (@{org.login})")
    except Exception as e:
        console.print(f"[red]Error: Could not find organization '{GITHUB_ORG}': {e}[/red]")
        sys.exit(1)
    
    console.print(f"Using {MAX_WORKERS} concurrent workers")
    console.print()
    
    # Get all repositories (handle pagination automatically)
    console.print("Fetching repository list...")
    repos = list(tqdm(
        org.get_repos(type='all'),
        desc="Discovering repositories",
        unit="repos"
    ))
    
    # Filter out archived repositories
    active_repos = [repo for repo in repos if not repo.archived]
    archived_count = len(repos) - len(active_repos)
    
    total_repos = len(active_repos)
    console.print(f"\nFound {total_repos} active repositories to process")
    if archived_count > 0:
        console.print(f"(Skipping {archived_count} archived repositories)")
    console.print()
    
    # Results storage
    results: List[ProcessResult] = []
    
    # Process repositories with thread pool
    with tqdm(total=total_repos, desc="Processing repositories", unit="repos") as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all tasks
            future_to_repo = {
                executor.submit(process_repository, repo, pbar): repo 
                for repo in active_repos
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_repo):
                result = future.result()
                results.append(result)
    
    # Display summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Total repositories processed: {total_repos}")
    print(f"  ✓ Updated: {stats['updated']}")
    print(f"  - Skipped: {stats['skipped']}")
    print(f"  ✗ Errors: {stats['errors']}")
    
    # Show updated repositories
    if stats['updated'] > 0:
        print(f"\n{'='*60}")
        print("UPDATED REPOSITORIES")
        print(f"{'='*60}")
        for result in sorted(results, key=lambda x: x.repo_name):
            if result.status == 'updated':
                print(f"  {result.repo_name}: {result.old_branch} → {result.new_branch}")
    
    # Show errors if any
    if stats['errors'] > 0:
        print(f"\n{'='*60}")
        print("ERRORS")
        print(f"{'='*60}")
        for result in sorted(results, key=lambda x: x.repo_name):
            if result.status == 'error':
                print(f"  {result.repo_name}: {result.message}")

if __name__ == "__main__":
    main()
