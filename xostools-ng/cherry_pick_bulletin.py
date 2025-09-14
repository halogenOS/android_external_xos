#!/usr/bin/env python3
"""
Cherry-pick security patches from Android Security Bulletins.

Cherry-picks security patches for a specific Android version from
Android Security Bulletins, with deferred pushing support.
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
from rich.table import Table

# Constants for commit prefixes
ALREADY_APPLIED_PREFIX = "[ALREADY APPLIED]"
NO_CHANGE_PREFIX = "[NO CHANGE]"
from dataclasses import dataclass
import threading
import tempfile
import git
import re

from xos_common import (
    ManifestParser,
    GitOperations,
    get_android_top,
    ProjectInfo,
    ProjectMapping,
    console,
    handle_lfs_cleanup,
    safe_add_or_update_remote,
    safe_get_remote,
    ensure_aosp_remote,
    run_repo_command,
    generate_manifest,
    cleanup_manifest,
    truncate_project_name,
    build_project_mappings,
    find_matching_projects,
    filter_patches_by_android_version,
    find_similar_commit_by_message,
    track_project_in_xos,
    find_project_in_default_manifest
)

from fetch_bulletin import get_bulletin_patches
from git_lock import GitRepoLock

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
class CherryPickTask:
    """A single cherry-pick task."""
    project_mapping: ProjectMapping
    patch_info: Dict[str, Any]
    onto: Optional[str] = None
    security_patch_level: Optional[str] = None
    branch_name: Optional[str] = None

@dataclass
class CherryPickResult:
    """Result of a cherry-pick operation."""
    project_path: str
    patch_ref: str
    patch_url: str
    success: bool
    message: str
    needs_push: bool = False
    push_command: Optional[str] = None
    had_lfs: bool = False
    used_existing_branch: bool = False
    verbose_only: bool = False


def setup_progress_bar(total_tasks: int, dry_run: bool, console):
    """Setup and return progress bar with main progress bar."""
    dry_run_prefix = "[DRY RUN] " if dry_run else ""

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console
    )

    # Main progress bar
    pick_task = progress.add_task(
        f"[cyan]Cherry-picking patches {dry_run_prefix.strip()}",
        total=total_tasks
    )

    return progress, pick_task


def group_tasks_by_repository(tasks):
    """Group tasks by repository to ensure serial execution within each repo."""
    repo_buckets = {}
    for task in tasks:
        repo_path = task.project_mapping.local_path
        if repo_path not in repo_buckets:
            repo_buckets[repo_path] = []
        repo_buckets[repo_path].append(task)
    return repo_buckets


def process_repo_bucket(repo_path, repo_tasks, dry_run, cherry_picker, progress, pick_task, stop_event):
    """Process all tasks for a single repository sequentially."""
    # Create a progress bar for this specific repository bucket
    bucket_task = progress.add_task(
        f"[dim]Starting {repo_path}...",
        total=len(repo_tasks)
    )

    bucket_results = []
    for i, task in enumerate(repo_tasks):
        if stop_event.is_set():
            break

        try:
            result = perform_single_cherry_pick(task, dry_run, cherry_picker, progress, bucket_task)
            bucket_results.append(result)
        except Exception as e:
            failed_result = CherryPickResult(
                task.project_mapping.local_path,
                task.patch_info['ref'],
                task.patch_info['url'],
                False,
                f"exception: {str(e)}"
            )
            bucket_results.append(failed_result)

        # Update both main progress and bucket progress
        progress.update(pick_task, advance=1)
        progress.update(bucket_task, advance=1)

    # Remove the bucket progress bar when done
    progress.remove_task(bucket_task)

    return bucket_results


def submit_cherry_pick_buckets(executor_pool, repo_buckets, dry_run, cherry_picker, progress, pick_task, stop_event):
    """Submit repository buckets to the executor pool."""
    return {
        executor_pool.submit(process_repo_bucket, repo_path, repo_tasks, dry_run, cherry_picker, progress, pick_task, stop_event): repo_path
        for repo_path, repo_tasks in repo_buckets.items()
    }


def process_completed_bucket_futures(futures, progress, stop_event, successful_picks, failed_picks, console):
    """Process completed bucket futures and update results."""
    for future in as_completed(futures):
        if stop_event.is_set():
            break

        try:
            repo_path = futures[future]
            bucket_results = future.result()

            # Process all results from this bucket
            for result in bucket_results:
                if result.success:
                    successful_picks.append(result)
                else:
                    failed_picks.append(result)

        except Exception as e:
            repo_path = futures[future]
            # Handle bucket-level exceptions - could affect multiple tasks
            console.print(f"[red]Exception processing repository bucket {repo_path}: {str(e)}[/red]")


def perform_single_cherry_pick(task: CherryPickTask, dry_run: bool, cherry_picker: 'BulletinCherryPicker' = None, progress=None, bucket_task=None) -> CherryPickResult:
    """Perform cherry-pick operation for a single patch."""
    mapping = task.project_mapping
    patch_info = task.patch_info
    patch_ref = patch_info['ref']
    patch_url = patch_info['url']
    onto = task.onto
    security_patch_level = task.security_patch_level
    branch_name = task.branch_name

    project_path = get_android_top() / mapping.local_path

    # Check if this repository is in conflict state
    if cherry_picker and not dry_run:
        with cherry_picker.conflict_lock:
            if mapping.local_path in cherry_picker.conflicted_repos:
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "skipped due to previous conflict in this repository", verbose_only=True)

    if not project_path.exists():
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "directory not found")

    try:
        # Acquire repository lock to prevent concurrent git operations
        with GitRepoLock(project_path):
            repo = git.Repo(project_path)

            # Check if there's already a merge conflict in progress
            if repo.git.status('--porcelain').strip():
                try:
                    # Check if we're in the middle of a merge/cherry-pick
                    merge_head_path = project_path / ".git" / "MERGE_HEAD"
                    cherry_pick_head_path = project_path / ".git" / "CHERRY_PICK_HEAD"

                    if merge_head_path.exists() or cherry_pick_head_path.exists():
                        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "repository has unresolved merge conflict - resolve or abort first")

                    # Check for unstaged changes that could interfere
                    status_output = repo.git.status('--porcelain')
                    if any(line.startswith(('UU', 'AA', 'DD')) for line in status_output.split('\n')):
                        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "repository has unresolved merge conflicts")

                except Exception:
                    # If we can't check status, assume there might be conflicts
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "unable to check repository status for conflicts")

            if dry_run:
                # Check current branch and status for better dry-run info
                try:
                    current_branch = repo.active_branch.name
                except TypeError:
                    current_branch = "detached HEAD"

                # Check if repo is shallow
                is_shallow = GitOperations.is_shallow_repo(project_path)
                shallow_info = " (shallow repo - would unshallow)" if is_shallow else ""

                # Check for LFS
                lfsconfig_exists = (project_path / ".lfsconfig").exists()
                gitattributes_has_lfs = False
                gitattributes_path = project_path / ".gitattributes"
                if gitattributes_path.exists():
                    with open(gitattributes_path, 'r') as f:
                        gitattributes_has_lfs = 'merge=lfs' in f.read()
                lfs_info = " (has LFS - would cleanup)" if (lfsconfig_exists or gitattributes_has_lfs) else ""

                # Add branch info if applicable
                branch_info = ""
                if onto and (security_patch_level or branch_name):
                    target_branch_name = branch_name if branch_name else f"{onto}-ASB-{security_patch_level}"
                    branch_info = f" onto branch {target_branch_name}"

                return CherryPickResult(
                    mapping.local_path,
                    patch_ref,
                    patch_url,
                    True,
                    f"would cherry-pick {patch_ref[:12]} from {mapping.aosp_name}{branch_info}{shallow_info}{lfs_info}"
                )

            # Add/update AOSP remote
            aosp_remote = ensure_aosp_remote(repo, mapping.aosp_name)

            # Check if shallow and unshallow if needed
            if GitOperations.is_shallow_repo(project_path):
                try:
                    repo = git.Repo(project_path)
                    # Unshallow from the main remote first
                    repo.git.fetch(mapping.remote, "--unshallow")
                except Exception as e:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to unshallow repository: {str(e)}")

            # Determine the target branch
            used_existing_branch = False
            if onto and (security_patch_level or branch_name):
                # Parse onto parameter - remote/ref format is required
                if '/' not in onto:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"--onto parameter must be in format 'remote/ref', got: {onto}")

                parts = onto.split('/', 1)
                onto_remote = parts[0]
                onto_ref = parts[1]

                # Ensure the remote exists and is added
                try:
                    if onto_remote == 'aosp':
                        # Ensure AOSP remote exists
                        remote_obj = ensure_aosp_remote(repo, mapping.aosp_name)
                    else:
                        # Try to get the remote
                        remote_obj = safe_get_remote(repo, onto_remote)
                        if not remote_obj:
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"remote '{onto_remote}' not found for onto parameter")

                    # Fetch the specific ref from the remote to ensure we have it
                    remote_obj.fetch(refspec=onto_ref)
                except Exception as e:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to fetch from remote '{onto_remote}': {str(e)}")

                # Use custom branch name if provided, otherwise generate ASB branch name using only the ref part
                base_onto_ref = onto_ref
                target_branch_name = branch_name if branch_name else f"{base_onto_ref}-ASB-{security_patch_level}"

                try:
                    current_branch = repo.active_branch.name
                except TypeError:
                    current_branch = None

                # Branch setup is handled upfront by setup_branches method
            else:
                # Original logic - use mapping.revision
                try:
                    current_branch = repo.active_branch.name
                except TypeError:
                    current_branch = None

                target_branch = mapping.revision

                if current_branch != target_branch:
                    try:
                        # Try to checkout existing branch
                        repo.git.checkout(target_branch)
                    except git.exc.GitCommandError:
                        try:
                            # Create and checkout new branch tracking remote
                            remote_ref = f"{mapping.remote}/{target_branch}"
                            repo.git.fetch(mapping.remote)
                            repo.git.checkout('-b', target_branch, remote_ref)
                            branch = repo.heads[target_branch]
                            branch.set_tracking_branch(repo.remotes[mapping.remote].refs[target_branch])
                        except git.exc.GitCommandError as e:
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to checkout branch {target_branch}: {str(e)}")

            # Perform the cherry-pick
            try:
                # Update bucket progress to show fetching
                if progress and bucket_task:
                    progress.update(bucket_task, description=f"[yellow]Fetching {mapping.local_path} ({patch_ref[:12]})")

                # Fetch from AOSP to get the commit
                aosp_remote = safe_get_remote(repo, 'aosp')
                if not aosp_remote:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "aosp remote not found")
                aosp_remote.fetch()

                # Update bucket progress to show cherry-picking
                if progress and bucket_task:
                    progress.update(bucket_task, description=f"[green]Cherry-picking {mapping.local_path} ({patch_ref[:12]})")

                # Check if commit already exists in current branch
                try:
                    # Check if the commit is already in the current branch history
                    repo.git.merge_base('--is-ancestor', patch_ref, 'HEAD')
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, "patch already applied (skipped)", needs_push=False)
                except git.exc.GitCommandError:
                    # Commit is not in current branch, proceed with cherry-pick
                    pass

                # Use fuzzy matching to find similar commits (>90% similarity)
                # When using --onto, only search in the upstream ref to avoid finding our own recent commits
                until_ref = onto_ref if onto else None
                existing_commit = find_similar_commit_by_message(repo, patch_ref, similarity_threshold=0.9, until_ref=until_ref)
                if existing_commit:
                    cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: Found similar commit: {existing_commit[:8]} for {patch_ref[:8]}")
                else:
                    cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: No similar commit found for {patch_ref[:8]} (fuzzy matching)")

                # Try cherry-pick
                try:
                    repo.git.cherry_pick(patch_ref, '--no-edit')
                except git.exc.GitCommandError as cherry_pick_error:
                    error_msg = str(cherry_pick_error)

                    # Check if it's just empty changes
                    is_empty_cherrypick = ("nothing to commit" in error_msg.lower() or "no changes added to commit" in error_msg.lower() or "would result in an empty commit" in error_msg.lower() or "the previous cherry-pick is now empty" in error_msg.lower())

                    if is_empty_cherrypick:
                        cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: Empty cherry-pick detected for {patch_ref[:8]}")
                        cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: existing_commit: {existing_commit[:8] if existing_commit else 'None'}")

                        # Check if we've already created an [ALREADY APPLIED] or [NO CHANGE] commit for this patch
                        original_commit = repo.commit(patch_ref)
                        patch_title = original_commit.message.split('\n')[0].strip()
                        already_applied_title = f"{ALREADY_APPLIED_PREFIX} {patch_title}"
                        no_change_title = f"{NO_CHANGE_PREFIX} {patch_title}"

                        commit_already_exists = False
                        if onto:
                            # Parse onto to get the ref
                            if '/' in onto:
                                onto_remote, onto_ref = onto.split('/', 1)
                            else:
                                onto_ref = onto

                            # Check if the [ALREADY APPLIED] or [NO CHANGE] title already exists in the range
                            # Do this in Python to avoid git grep pattern issues with special characters
                            try:
                                commits_in_range = list(repo.iter_commits(f"{onto_ref}..HEAD"))
                                for commit in commits_in_range:
                                    commit_title = commit.message.split('\n')[0].strip()
                                    if commit_title == already_applied_title or commit_title == no_change_title:
                                        commit_already_exists = True
                                        prefix = ALREADY_APPLIED_PREFIX if commit_title == already_applied_title else NO_CHANGE_PREFIX
                                        cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: {prefix} commit already exists for {patch_ref[:8]}")
                                        break
                            except git.exc.GitCommandError:
                                pass

                        if commit_already_exists:
                            # Skip creating another commit
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, "commit already exists (skipped)", needs_push=False)

                        # Abort the cherry-pick and clean up
                        try:
                            repo.git.cherry_pick('--abort')
                            repo.git.reset('--hard', 'HEAD')
                        except:
                            pass

                        # Determine commit type and message based on evidence
                        if existing_commit:
                            # We found a similar commit via fuzzy matching - this is [ALREADY APPLIED]
                            cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: Using fuzzy-matched commit: {existing_commit[:8]}")
                            commit_prefix = ALREADY_APPLIED_PREFIX
                            commit_msg = f"{ALREADY_APPLIED_PREFIX} {patch_title}\n\nOriginal commit was already applied in {existing_commit}\nCherry-picked from: {patch_ref}"
                            success_msg = f"{ALREADY_APPLIED_PREFIX} commit created successfully"
                        else:
                            # No similar commit found - this is [NO CHANGE] (empty cherry-pick with no evidence of existing commit)
                            cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: No similar commit found - using {NO_CHANGE_PREFIX}")
                            commit_prefix = NO_CHANGE_PREFIX
                            commit_msg = f"{NO_CHANGE_PREFIX} {patch_title}\n\nCherry-pick resulted in no changes - commit may no longer be applicable\nCherry-picked from: {patch_ref}"
                            success_msg = f"{NO_CHANGE_PREFIX} commit created successfully"

                        # Create empty commit
                        try:
                            # Use original author and author date
                            author_name = original_commit.author.name
                            author_email = original_commit.author.email
                            author_date = original_commit.authored_datetime.strftime('%Y-%m-%d %H:%M:%S %z')

                            repo.git.commit('--allow-empty', '-m', commit_msg,
                                           f'--author={author_name} <{author_email}>',
                                           f'--date={author_date}')
                            cherry_picker.verbose_print(f"  [green]→[/green] {mapping.local_path}: Created {commit_prefix} commit for {patch_ref[:8]}")

                            # Return success for both [ALREADY APPLIED] and [NO CHANGE] commits
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, success_msg, needs_push=True)
                        except git.exc.GitCommandError as commit_error:
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to create {commit_prefix} commit: {str(commit_error)}")
                    else:
                        # Re-raise the original error for normal handling
                        raise cherry_pick_error

            except git.exc.GitCommandError as e:
                error_msg = str(e)

                # Mark this repository as conflicted for future cherry-picks
                if cherry_picker and not dry_run:
                    with cherry_picker.conflict_lock:
                        cherry_picker.conflicted_repos.add(mapping.local_path)

                if "is a merge but no -m option was given" in error_msg:
                    try:
                        # Try cherry-picking merge commit with -m 1
                        repo.git.cherry_pick(patch_ref, '-m', '1', '--no-edit')
                        # If successful, remove from conflicted repos
                        if cherry_picker and not dry_run:
                            with cherry_picker.conflict_lock:
                                cherry_picker.conflicted_repos.discard(mapping.local_path)
                    except git.exc.GitCommandError as e2:
                        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick failed (merge commit): {str(e2)}")
                elif "bad object" in error_msg or "unknown revision" in error_msg:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"commit not found in AOSP repository (may be different repo)")
                else:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick failed: {error_msg}")

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
                lfs_success, lfs_msg = handle_lfs_cleanup(project_path, dry_run)
                if not lfs_success:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick succeeded but LFS cleanup failed: {lfs_msg}")

            # Prepare push command for later execution
            if onto and (security_patch_level or branch_name):
                # For ASB/custom branches, push the current branch
                target_branch_name = branch_name if branch_name else f"{onto}-ASB-{security_patch_level}"
                push_cmd = f"git push {mapping.remote} {target_branch_name}"
            else:
                # Original logic for regular branches
                target_branch = mapping.revision
                push_cmd = f"git push {mapping.remote} HEAD:{target_branch}"

            return CherryPickResult(
                mapping.local_path,
                patch_ref,
                patch_url,
                True,
                "cherry-pick completed successfully",
                needs_push=True,
                push_command=push_cmd,
                had_lfs=had_lfs,
                used_existing_branch=used_existing_branch
            )

    except Exception as e:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"unexpected error: {str(e)}")


class BulletinCherryPicker:
    def __init__(self, bulletin_dates: List[str], android_version: str, dry_run: bool = False, max_workers: int = 4, push_only: bool = False, onto: Optional[str] = None, branch_name: Optional[str] = None, force_recreate: bool = False, verbose: bool = False):
        self.bulletin_dates = bulletin_dates
        self.android_version = android_version
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.push_only = push_only
        self.onto = onto
        self.branch_name = branch_name
        self.force_recreate = force_recreate
        self.verbose = verbose
        self.top = get_android_top()
        self.conflicted_repos = set()  # Track repos with conflicts
        self.conflict_lock = threading.Lock()  # Thread-safe access to conflicted_repos

    def verbose_print(self, message):
        """Print message only if verbose mode is enabled."""
        if self.verbose:
            console.print(message)

    def setup_branches(self, tasks: List[CherryPickTask], project_mappings: Dict[str, ProjectMapping]):
        """Setup branches in all repositories that will be cherry-picked to."""
        if not self.onto or not self.branch_name:
            return

        # Parse the onto parameter
        if '/' in self.onto:
            onto_remote, onto_ref = self.onto.split('/', 1)
        else:
            return  # Invalid onto format

        # Get unique repositories from tasks
        unique_repos = set()
        for task in tasks:
            unique_repos.add(task.project_mapping.local_path)

        if self.force_recreate:
            console.print(f"[cyan]Recreating branch '{self.branch_name}' in {len(unique_repos)} repositories...[/cyan]")
        else:
            console.print(f"[cyan]Setting up branches in {len(unique_repos)} repositories...[/cyan]")

        for repo_path in unique_repos:
            try:
                import git
                from pathlib import Path
                project_path = Path(self.top) / repo_path
                if not project_path.exists():
                    continue

                repo = git.Repo(project_path)

                # Check if the branch exists
                existing_branch = None
                for branch in repo.branches:
                    if branch.name == self.branch_name:
                        existing_branch = branch
                        break

                # Check if the ref is a tag (tags don't use remote prefix)
                is_tag = False
                try:
                    repo.git.show_ref('--tags', f'refs/tags/{onto_ref}')
                    is_tag = True
                except git.exc.GitCommandError:
                    pass

                checkout_ref = onto_ref if is_tag else self.onto

                # Check current branch
                current_branch = None
                try:
                    current_branch = repo.active_branch.name
                except TypeError:
                    pass

                if existing_branch:
                    # Branch exists
                    if self.force_recreate:
                        # Force recreate: checkout and reset it
                        if current_branch != self.branch_name:
                            repo.git.checkout(self.branch_name)
                        repo.git.reset('--hard', checkout_ref)
                        self.verbose_print(f"  [yellow]→[/yellow] Reset {repo_path}:{self.branch_name} to {checkout_ref}")
                    else:
                        # Just checkout existing branch if not already on it
                        if current_branch != self.branch_name:
                            repo.git.checkout(self.branch_name)
                            self.verbose_print(f"  [yellow]→[/yellow] Checked out existing {repo_path}:{self.branch_name}")
                        else:
                            self.verbose_print(f"  [yellow]→[/yellow] Already on {repo_path}:{self.branch_name}")
                else:
                    # Branch doesn't exist, create and checkout it
                    repo.git.checkout('-b', self.branch_name, checkout_ref)
                    self.verbose_print(f"  [yellow]→[/yellow] Created {repo_path}:{self.branch_name} from {checkout_ref}")

            except Exception as e:
                self.verbose_print(f"  [red]→[/red] Failed to recreate branch in {repo_path}: {str(e)}")

    def update_security_patch_level(self, latest_bulletin_date: str):
        """Update security patch level after successful cherry-picking."""
        import os
        from pathlib import Path

        custom_product_dir = os.getenv('CUSTOM_PRODUCT_DIR')
        if not custom_product_dir:
            console.print("[yellow]CUSTOM_PRODUCT_DIR not set, skipping security patch level update[/yellow]")
            return False

        # Find the release codename directory in product/halogenOS/release/flag_values/
        release_flag_values_dir = Path(custom_product_dir) / "release/flag_values"
        if not release_flag_values_dir.exists():
            console.print(f"[yellow]No release directory found at {release_flag_values_dir}, skipping security patch level update[/yellow]")
            return False

        # Find the release codename (should be only one directory)
        release_codename = None
        for item in release_flag_values_dir.iterdir():
            if item.is_dir():
                release_codename = item.name
                break

        if not release_codename:
            console.print(f"[red]Could not find release codename directory in {release_flag_values_dir}[/red]")
            return False

        console.print(f"[cyan]Found release codename: {release_codename}[/cyan]")

        # Path to the security patch file
        security_patch_file = release_flag_values_dir / release_codename / "RELEASE_PLATFORM_SECURITY_PATCH.textproto"

        # Content for the security patch file (following the format from build/release)
        content = f'''name: "RELEASE_PLATFORM_SECURITY_PATCH"
value {{
  string_value: "{latest_bulletin_date}"
}}
'''

        try:
            if not self.dry_run:
                # Create directory if it doesn't exist
                security_patch_file.parent.mkdir(parents=True, exist_ok=True)

                # Write the security patch file
                with open(security_patch_file, 'w') as f:
                    f.write(content)

                console.print(f"[green]Updated security patch level to {latest_bulletin_date} in {security_patch_file}[/green]")
            else:
                console.print(f"[blue]Would update security patch level to {latest_bulletin_date} in {security_patch_file}[/blue]")

            return True

        except Exception as e:
            console.print(f"[red]Failed to update security patch level: {e}[/red]")
            return False

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        return run_repo_command(command, self.top, self.dry_run)

    def generate_manifest(self) -> Path:
        """Generate temporary manifest file."""
        return generate_manifest(self.top, self.dry_run)

    def create_cherry_pick_tasks(self, bulletin_data: List[Dict], patches: List[Dict], project_mappings: Dict[str, ProjectMapping]) -> List[CherryPickTask]:
        """Create cherry-pick tasks from filtered patches."""
        tasks = []
        unmatched_patches = []

        # Extract security patch level from bulletin data
        security_patch_level = None
        if bulletin_data:
            # Get the first patch level (assuming we're only processing one)
            security_patch_level = bulletin_data[0].get('security_patch_level')

        for patch in patches:
            patch_repo_name = patch.get('name', '')
            matching_projects = find_matching_projects(patch_repo_name, project_mappings)
            if not matching_projects:
                unmatched_patches.append(patch_repo_name)
                continue

            # Create task for each matching project (in case of multiple matches)
            for mapping in matching_projects:
                # Validate project exists in default manifest and track it before creating task
                if find_project_in_default_manifest(mapping.local_path) is None:
                    console.print(f"[red]FATAL: Project {mapping.local_path} not found in default manifest[/red]")
                    console.print(f"[red]This indicates a serious issue. Exiting to keep tree clean.[/red]")
                    sys.exit(1)

                # Track the project in XOS manifests
                track_project_in_xos(mapping.local_path, dry_run=self.dry_run)

                task = CherryPickTask(
                    project_mapping=mapping,
                    patch_info=patch,
                    onto=self.onto,
                    security_patch_level=security_patch_level,
                    branch_name=self.branch_name
                )
                tasks.append(task)

        if unmatched_patches:
            # Remove duplicates while preserving order
            unique_unmatched = []
            seen = set()
            for repo_name in unmatched_patches:
                if repo_name not in seen:
                    unique_unmatched.append(repo_name)
                    seen.add(repo_name)

            console.print(f"[yellow]Warning: No local projects found for {len(unique_unmatched)} patches:[/yellow]")
            for repo_name in unique_unmatched:
                console.print(f"  [yellow]- {repo_name}[/yellow]")

        return tasks

    def execute_pushes(self, successful_picks: List[CherryPickResult]) -> Tuple[int, List[Tuple[str, str]]]:
        """Execute all push operations at the end."""
        # Filter out results that don't need pushing (e.g., already applied patches)
        pushable_picks = [pick for pick in successful_picks if pick.needs_push and pick.push_command]

        if self.dry_run:
            console.print(f"[blue][DRY RUN] Would push {len(pushable_picks)} repositories[/blue]")
            return len(pushable_picks), []

        if not pushable_picks:
            console.print("[blue]No repositories need pushing (all patches were already applied)[/blue]")
            return 0, []

        console.print(f"\n[bold cyan]Pushing {len(pushable_picks)} repositories with new cherry-picks...[/bold cyan]")

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

            push_task = progress.add_task("[cyan]Pushing changes", total=len(pushable_picks))

            for result in pushable_picks:
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

    def process_cherry_picks(self, tasks: List[CherryPickTask]) -> Tuple[List[CherryPickResult], List[CherryPickResult]]:
        """Process all cherry-pick tasks using multi-threading."""
        global executor

        if not tasks:
            console.print("[yellow]No cherry-pick tasks to process[/yellow]")
            return [], []

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        self.verbose_print(f"\n[bold]{dry_run_prefix}Processing {len(tasks)} cherry-pick operations...[/bold]")

        successful_picks = []
        failed_picks = []

        # Setup progress bar
        progress, pick_task = setup_progress_bar(len(tasks), self.dry_run, console)

        # Group tasks by repository
        repo_buckets = group_tasks_by_repository(tasks)

        # Process with progress bar and threading
        with progress:
            # Limit workers to number of repository buckets to avoid over-threading
            max_workers = min(self.max_workers, len(repo_buckets))
            with ThreadPoolExecutor(max_workers=max_workers) as executor_pool:
                # Submit repository buckets
                futures = submit_cherry_pick_buckets(executor_pool, repo_buckets, self.dry_run, self, progress, pick_task, stop_event)

                # Process results as they complete
                process_completed_bucket_futures(futures, progress, stop_event, successful_picks, failed_picks, console)

                # Shutdown if stopped
                if stop_event.is_set():
                    executor_pool.shutdown(wait=False, cancel_futures=True)

        return successful_picks, failed_picks

    def display_cherry_pick_results(self, successful_picks: List[CherryPickResult], failed_picks: List[CherryPickResult]):
        """Display cherry-pick results in a formatted table."""
        if not successful_picks and not failed_picks:
            return

        # Display successful cherry-picks
        if successful_picks:
            console.print(f"\n[bold green]Successful cherry-picks ({len(successful_picks)}):[/bold green]")

            table = Table(show_header=True, header_style="bold blue")
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Patch", style="yellow", no_wrap=True)
            table.add_column("Message", style="green")
            table.add_column("Branch", style="magenta", no_wrap=True)
            table.add_column("", width=2)  # Status column for emoji

            for result in successful_picks:
                emoji = "✅" if result.needs_push else "⏭️"  # Skip emoji for already applied patches
                branch_status = "existing" if result.used_existing_branch else "new"
                table.add_row(
                    truncate_project_name(result.project_path),
                    result.patch_ref[:12],  # Show first 12 chars of commit hash
                    result.message,
                    branch_status,
                    emoji
                )

            console.print(table)

        # Display failed cherry-picks
        if failed_picks:
            console.print(f"\n[bold red]Failed cherry-picks ({len(failed_picks)}):[/bold red]")

            fail_table = Table(show_header=True, header_style="bold red")
            fail_table.add_column("Project", style="cyan", no_wrap=True)
            fail_table.add_column("Patch", style="yellow", no_wrap=True)
            fail_table.add_column("Error", style="red")
            fail_table.add_column("", width=2)  # Status column for emoji

            for result in failed_picks:
                fail_table.add_row(
                    truncate_project_name(result.project_path),
                    result.patch_ref[:12],
                    result.message,
                    "❌"
                )

            console.print(fail_table)

    def run(self):
        """Main execution flow."""
        try:
            # Fetch bulletin patches from multiple dates
            all_bulletin_data = []
            all_patches = []

            for bulletin_date in self.bulletin_dates:
                console.print(f"[cyan]Fetching security bulletin for {bulletin_date}...[/cyan]")
                bulletin_data = get_bulletin_patches(bulletin_date)
                all_bulletin_data.extend(bulletin_data)

                # Filter patches for Android version
                console.print(f"[cyan]Filtering patches for Android {self.android_version} from {bulletin_date}...[/cyan]")
                patches = filter_patches_by_android_version(bulletin_data, self.android_version)
                all_patches.extend(patches)

                if patches:
                    console.print(f"[green]Found {len(patches)} patches for Android {self.android_version} in {bulletin_date}[/green]")
                else:
                    console.print(f"[yellow]No patches found for Android {self.android_version} in {bulletin_date}[/yellow]")

            if not all_patches:
                console.print(f"[yellow]No patches found for Android {self.android_version} in any of the specified bulletins[/yellow]")
                return 0

            console.print(f"[green]Total patches found: {len(all_patches)} across {len(self.bulletin_dates)} bulletins[/green]")

            # Use the combined data for processing
            bulletin_data = all_bulletin_data
            patches = all_patches

            console.print(f"[green]Processing {len(patches)} patches for Android {self.android_version}[/green]")

        except Exception as e:
            console.print(f"[red]Failed to fetch bulletin data: {e}[/red]")
            return 1

        # Generate manifest and parse projects
        try:
            self.verbose_print("[cyan]Generating temporary manifest file...[/cyan]")
            manifest_path = self.generate_manifest()

            self.verbose_print("[cyan]Building project mappings from manifest...[/cyan]")
            project_mappings = build_project_mappings(manifest_path)
            self.verbose_print(f"[green]Built {len(project_mappings)} project mappings[/green]")

            self.verbose_print("[cyan]Creating cherry-pick tasks...[/cyan]")
            tasks = self.create_cherry_pick_tasks(bulletin_data, patches, project_mappings)
            self.verbose_print(f"[green]Created {len(tasks)} cherry-pick tasks[/green]")

            # Setup branches if using --onto (before any cherry-picking begins)
            if self.onto and self.branch_name:
                if self.force_recreate:
                    console.print(f"[cyan]Force recreating branches in all repositories...[/cyan]")
                else:
                    console.print(f"[cyan]Setting up branches in all repositories...[/cyan]")
                self.setup_branches(tasks, project_mappings)

        except Exception as e:
            console.print(f"[red]Failed to prepare cherry-pick tasks: {e}[/red]")
            return 1

        # Process cherry-picks (skip if push-only mode)
        if self.push_only:
            console.print(f"[blue]Push-only mode: Assuming all {len(tasks)} patches are already cherry-picked[/blue]")
            # In push-only mode, we would need to scan for existing commits
            # For now, we'll just skip cherry-picking
            successful_picks = []
            failed_picks = []
        else:
            successful_picks, failed_picks = self.process_cherry_picks(tasks)

        # Display results
        self.display_cherry_pick_results(successful_picks, failed_picks)

        # Execute pushes for successful cherry-picks (only when explicitly requested)
        push_successes = 0
        push_failures = []

        # Only push when --push-only is specified
        if successful_picks and not self.dry_run and self.push_only:
            push_successes, push_failures = self.execute_pushes(successful_picks)

        # Clean up temporary manifest
        cleanup_manifest(manifest_path, self.dry_run)

        # Summary
        console.print("\n" + "=" * 60)
        console.print("[bold]Security bulletin cherry-pick complete![/bold]")
        console.print(f"[green]Successfully cherry-picked:[/green] {len(successful_picks)}/{len(tasks)} patches")

        if not self.dry_run and successful_picks:
            console.print(f"[green]Successfully pushed:[/green] {push_successes}/{len([p for p in successful_picks if p.needs_push])} repositories")

        if failed_picks:
            console.print(f"\n[red]Failed cherry-picks ({len(failed_picks)}):[/red]")
            for result in failed_picks:
                # Show message if it's not verbose-only, or if we're in verbose mode
                if not result.verbose_only or self.verbose:
                    console.print(f"  [red]- {result.project_path} ({result.patch_ref[:12]}):[/red] {result.message}")

        if push_failures:
            console.print(f"\n[red]Failed pushes ({len(push_failures)}):[/red]")
            for project_path, error in push_failures:
                console.print(f"  [red]- {project_path}:[/red] {error}")

        # Update security patch level if all cherry-picks were successful
        if successful_picks and not failed_picks:
            # Find the latest bulletin date from the processed dates
            latest_bulletin_date = max(self.bulletin_dates)
            console.print(f"\n[cyan]Updating security patch level to {latest_bulletin_date}...[/cyan]")
            self.update_security_patch_level(latest_bulletin_date)

        console.print("\n[bold green]Everything done.[/bold green]")

        # Return error code if there were failures
        return 1 if (failed_picks or push_failures) else 0


def main():
    parser = argparse.ArgumentParser(
        description="Cherry-pick security patches from Android Security Bulletins",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 2025-09-01 --android-version 15          # Cherry-pick patches for Android 15
  %(prog)s 2025-09-01 --android-version 16 --dry-run  # Show what would be cherry-picked
  %(prog)s 2025-09-01 --android-version 15 --push-only  # Only push existing cherry-picks
  %(prog)s 2025-09-01 --android-version 15 --onto aosp/android-16.0.0_r1  # Auto-generated ASB branches
  %(prog)s 2025-07-01 2025-08-01 --android-version 16 --onto aosp/android-16.0.0_r1 --branch-name android-16.0.0_r1-ASB_2025-08-01  # Custom branch name
        """
    )

    parser.add_argument(
        "bulletin_dates",
        nargs="+",
        help="One or more bulletin dates in YYYY-MM-DD format"
    )

    parser.add_argument(
        "--android-version",
        required=True,
        help="Android version (e.g., 15, 16)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making any changes"
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show verbose output including detailed progress information"
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
        help="Only push repositories, skip cherry-picking"
    )

    parser.add_argument(
        "--onto",
        type=str,
        help="Use specified remote/ref as base. Format: remote/ref (e.g., aosp/android-16.0.0_r1). Creates branch '<ref>-ASB-<security_patch_level>' unless --branch-name is specified."
    )

    parser.add_argument(
        "--branch-name",
        type=str,
        help="Custom branch name to use instead of auto-generated ASB branch name"
    )

    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="Force recreate the branch specified by --branch-name even if it already exists"
    )

    args = parser.parse_args()

    try:
        picker = BulletinCherryPicker(
            bulletin_dates=args.bulletin_dates,
            android_version=args.android_version,
            dry_run=args.dry_run,
            max_workers=args.workers,
            push_only=args.push_only,
            onto=args.onto,
            branch_name=args.branch_name,
            force_recreate=args.force_recreate,
            verbose=args.verbose
        )

        return picker.run()

    except KeyboardInterrupt:
        console.print("\n[red]Aborted by user[/red]")
        return 130
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())