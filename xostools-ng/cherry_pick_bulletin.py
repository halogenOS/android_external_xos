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
    find_project_in_default_manifest,
    create_xos_repo,
    get_project_path,
    safe_add_or_update_remote,
    check_if_xos_repo_exists,
    get_xos_target_branch_for_tracked_project,
    setup_tracked_project_branch
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
    push_remote: Optional[str] = None
    push_local_branch: Optional[str] = None
    push_remote_branch: Optional[str] = None
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


def create_failed_result_from_exception(task, exception):
    """Create a failed CherryPickResult from an exception."""
    return CherryPickResult(
        task.project_mapping.local_path,
        task.patch_info['ref'],
        task.patch_info['url'],
        False,
        f"exception: {str(exception)}"
    )


def process_single_task_safely(task, dry_run, cherry_picker, progress, bucket_task):
    """Process a single task with exception handling."""
    try:
        return perform_single_cherry_pick(task, dry_run, cherry_picker, progress, bucket_task)
    except Exception as e:
        return create_failed_result_from_exception(task, e)


def process_repo_bucket(repo_path, repo_tasks, dry_run, cherry_picker, progress, pick_task, stop_event):
    """Process all tasks for a single repository sequentially."""
    bucket_task = progress.add_task(
        f"[dim]Starting {repo_path}...",
        total=len(repo_tasks)
    )

    bucket_results = []
    for i, task in enumerate(repo_tasks):
        if stop_event.is_set():
            break

        result = process_single_task_safely(task, dry_run, cherry_picker, progress, bucket_task)
        bucket_results.append(result)

        progress.update(pick_task, advance=1)
        progress.update(bucket_task, advance=1)

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


def check_conflict_state(cherry_picker, mapping, patch_ref, patch_url, dry_run):
    """Check if repository is in conflict state."""
    if cherry_picker and not dry_run:
        with cherry_picker.conflict_lock:
            if mapping.local_path in cherry_picker.conflicted_repos:
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "skipped due to previous conflict in this repository", verbose_only=True)
    return None


def check_merge_conflicts(repo, project_path, mapping, patch_ref, patch_url):
    """Check for existing merge conflicts in repository."""
    if repo.git.status('--porcelain').strip():
        try:
            merge_head_path = project_path / ".git" / "MERGE_HEAD"
            cherry_pick_head_path = project_path / ".git" / "CHERRY_PICK_HEAD"

            if merge_head_path.exists() or cherry_pick_head_path.exists():
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "repository has unresolved merge conflict - resolve or abort first")

            status_output = repo.git.status('--porcelain')
            if any(line.startswith(('UU', 'AA', 'DD')) for line in status_output.split('\n')):
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "repository has unresolved merge conflicts")

        except Exception:
            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "unable to check repository status for conflicts")
    return None


def create_dry_run_result(mapping, patch_ref, patch_url, project_path, onto, security_patch_level, branch_name):
    """Create dry-run result with repository information."""
    try:
        current_branch = "detached HEAD"
        try:
            repo = git.Repo(project_path)
            current_branch = repo.active_branch.name
        except TypeError:
            pass

        is_shallow = GitOperations.is_shallow_repo(project_path)
        shallow_info = " (shallow repo - would unshallow)" if is_shallow else ""

        lfsconfig_exists = (project_path / ".lfsconfig").exists()
        gitattributes_has_lfs = False
        gitattributes_path = project_path / ".gitattributes"
        if gitattributes_path.exists():
            with open(gitattributes_path, 'r') as f:
                gitattributes_has_lfs = 'merge=lfs' in f.read()
        lfs_info = " (has LFS - would cleanup)" if (lfsconfig_exists or gitattributes_has_lfs) else ""

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
    except Exception as e:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"dry-run check failed: {str(e)}")


def setup_repository(repo, mapping, project_path):
    """Setup repository by ensuring AOSP remote and unshallowing if needed."""
    aosp_remote = ensure_aosp_remote(repo, mapping.aosp_name)

    if GitOperations.is_shallow_repo(project_path):
        try:
            repo = git.Repo(project_path)
            repo.git.fetch(mapping.remote, "--unshallow")
        except Exception as e:
            raise Exception(f"failed to unshallow repository: {str(e)}")

    return aosp_remote


def find_remote_for_branch(repo, branch_name):
    """Find which remote has the given branch."""
    for remote in repo.remotes:
        try:
            remote.fetch(refspec=branch_name)
            return remote
        except git.exc.GitCommandError:
            continue
    return None


def setup_onto_branch(repo, onto, mapping, patch_ref, patch_url):
    """Setup branch when using --onto parameter.

    Accepts either 'remote/ref' format or a bare branch name.
    """
    if '/' not in onto:
        # Bare branch name — check if it exists locally first
        try:
            repo.git.rev_parse('--verify', onto)
            return onto, None
        except git.exc.GitCommandError:
            pass

        # Not local — find and fetch from upstream
        remote_obj = find_remote_for_branch(repo, onto)
        if not remote_obj:
            project_path = get_android_top() / mapping.local_path
            success, msg = setup_tracked_project_branch(project_path, mapping.local_path, onto)
            if success:
                return onto, None
            return None, CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"branch '{onto}' not found on any remote; upstream setup failed: {msg}")
        return onto, None

    parts = onto.split('/', 1)
    onto_remote = parts[0]
    onto_ref = parts[1]

    try:
        if onto_remote == 'aosp':
            remote_obj = ensure_aosp_remote(repo, mapping.aosp_name)
        else:
            remote_obj = safe_get_remote(repo, onto_remote)
            if not remote_obj:
                return None, CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"remote '{onto_remote}' not found for onto parameter")

        remote_obj.fetch(refspec=onto_ref)
        return onto_ref, None
    except Exception as e:
        return None, CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to fetch from remote '{onto_remote}': {str(e)}")


def setup_default_branch(repo, mapping, patch_ref, patch_url):
    """Setup default branch checkout."""
    try:
        current_branch = repo.active_branch.name
    except TypeError:
        current_branch = None

    target_branch = mapping.revision

    if current_branch != target_branch:
        try:
            repo.git.checkout(target_branch)
        except git.exc.GitCommandError:
            try:
                remote_ref = f"{mapping.remote}/{target_branch}"
                repo.git.fetch(mapping.remote)
                repo.git.checkout('-b', target_branch, remote_ref)
                branch = repo.heads[target_branch]
                branch.set_tracking_branch(repo.remotes[mapping.remote].refs[target_branch])
            except git.exc.GitCommandError as e:
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to checkout branch {target_branch}: {str(e)}")
    return None


def find_upstream_ref(repo, onto_ref, mapping):
    """Find the upstream remote tracking ref for the given branch."""
    try:
        tracking = repo.active_branch.tracking_branch()
        if tracking:
            return str(tracking)
    except (TypeError, ValueError):
        pass
    for remote_name in ('XOS', 'origin', mapping.remote):
        try:
            repo.git.rev_parse('--verify', f'{remote_name}/{onto_ref}')
            return f'{remote_name}/{onto_ref}'
        except git.exc.GitCommandError:
            continue
    return None


def check_already_handled(repo, patch_ref, mapping, patch_url, onto_ref, cherry_picker):
    """Check if commit is already handled: exists between upstream..HEAD,
    or has an [ALREADY APPLIED]/[NO CHANGE] marker commit."""
    original_commit = repo.commit(patch_ref)
    patch_title = original_commit.message.split('\n')[0].strip()
    already_applied_title = f"{ALREADY_APPLIED_PREFIX} {patch_title}"
    no_change_title = f"{NO_CHANGE_PREFIX} {patch_title}"

    # Determine the range to search (upstream..HEAD, or just HEAD if no upstream)
    upstream_ref = find_upstream_ref(repo, onto_ref, mapping) if onto_ref else None
    search_range = f"{upstream_ref}..HEAD" if upstream_ref else "HEAD"

    try:
        for commit in repo.iter_commits(search_range, max_count=200):
            title = commit.message.split('\n')[0].strip()
            if title == patch_title or title == already_applied_title or title == no_change_title:
                if cherry_picker:
                    cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: already handled: {title}")
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, "commit already exists (skipped)", needs_push=False)
    except git.exc.GitCommandError:
        pass

    # Also check exact ancestor
    try:
        repo.git.merge_base('--is-ancestor', patch_ref, 'HEAD')
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, "patch already applied (skipped)", needs_push=False)
    except git.exc.GitCommandError:
        pass

    return None


def create_already_applied_commit(repo, patch_ref, existing_commit, cherry_picker, mapping, patch_url):
    """Create an [ALREADY APPLIED] empty commit for a patch found in upstream."""
    original_commit = repo.commit(patch_ref)
    patch_title = original_commit.message.split('\n')[0].strip()

    # Abort any in-progress cherry-pick
    try:
        repo.git.cherry_pick('--abort')
    except:
        pass
    try:
        repo.git.reset('--hard', 'HEAD')
    except:
        pass

    commit_msg = f"{ALREADY_APPLIED_PREFIX} {patch_title}\n\nOriginal commit was already applied in {existing_commit}\nCherry-picked from: {patch_ref}"
    author_name = original_commit.author.name
    author_email = original_commit.author.email
    author_date = original_commit.authored_datetime.strftime('%Y-%m-%d %H:%M:%S %z')

    try:
        repo.git.commit('--allow-empty', '-m', commit_msg,
                       f'--author={author_name} <{author_email}>',
                       f'--date={author_date}')
        if cherry_picker:
            cherry_picker.verbose_print(f"  [green]→[/green] {mapping.local_path}: Created {ALREADY_APPLIED_PREFIX} commit for {patch_ref[:8]}")
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, f"{ALREADY_APPLIED_PREFIX} commit created successfully", needs_push=True)
    except git.exc.GitCommandError as e:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to create {ALREADY_APPLIED_PREFIX} commit: {str(e)}")


def create_no_change_commit(repo, patch_ref, cherry_picker, mapping, patch_url):
    """Create a [NO CHANGE] empty commit for a patch that results in no changes."""
    original_commit = repo.commit(patch_ref)
    patch_title = original_commit.message.split('\n')[0].strip()

    # Abort any in-progress cherry-pick
    try:
        repo.git.cherry_pick('--abort')
    except:
        pass
    try:
        repo.git.reset('--hard', 'HEAD')
    except:
        pass

    commit_msg = f"{NO_CHANGE_PREFIX} {patch_title}\n\nCherry-pick resulted in no changes - commit may no longer be applicable\nCherry-picked from: {patch_ref}"
    author_name = original_commit.author.name
    author_email = original_commit.author.email
    author_date = original_commit.authored_datetime.strftime('%Y-%m-%d %H:%M:%S %z')

    try:
        repo.git.commit('--allow-empty', '-m', commit_msg,
                       f'--author={author_name} <{author_email}>',
                       f'--date={author_date}')
        if cherry_picker:
            cherry_picker.verbose_print(f"  [green]→[/green] {mapping.local_path}: Created {NO_CHANGE_PREFIX} commit for {patch_ref[:8]}")
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, f"{NO_CHANGE_PREFIX} commit created successfully", needs_push=True)
    except git.exc.GitCommandError as e:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to create {NO_CHANGE_PREFIX} commit: {str(e)}")


def handle_cherry_pick_error(error_msg, repo, patch_ref, cherry_picker, mapping, patch_url):
    """Handle cherry-pick git command errors."""
    if cherry_picker:
        with cherry_picker.conflict_lock:
            cherry_picker.conflicted_repos.add(mapping.local_path)

    if "is a merge but no -m option was given" in error_msg:
        try:
            repo.git.cherry_pick(patch_ref, '-m', '1', '--no-edit')
            if cherry_picker:
                with cherry_picker.conflict_lock:
                    cherry_picker.conflicted_repos.discard(mapping.local_path)
            return None
        except git.exc.GitCommandError as e2:
            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick failed (merge commit): {str(e2)}")
    elif "bad object" in error_msg or "unknown revision" in error_msg:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"commit not found in AOSP repository (may be different repo)")
    else:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick failed: {error_msg}")


def handle_lfs_cleanup_if_needed(project_path, mapping, patch_ref, patch_url, dry_run):
    """Handle LFS cleanup if repository has LFS configuration."""
    lfsconfig_exists = (project_path / ".lfsconfig").exists()
    gitattributes_has_lfs = False

    gitattributes_path = project_path / ".gitattributes"
    if gitattributes_path.exists():
        with open(gitattributes_path, 'r') as f:
            gitattributes_has_lfs = 'merge=lfs' in f.read()

    had_lfs = lfsconfig_exists or gitattributes_has_lfs
    if had_lfs:
        lfs_success, lfs_msg = handle_lfs_cleanup(project_path, dry_run)
        if not lfs_success:
            return None, CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"cherry-pick succeeded but LFS cleanup failed: {lfs_msg}")
    return had_lfs, None


def get_push_parameters(onto, security_patch_level, branch_name, mapping):
    """Get push parameters for GitPython based on branching strategy."""
    if onto and (security_patch_level or branch_name):
        target_branch_name = branch_name if branch_name else f"{onto}-ASB-{security_patch_level}"
        return "XOS", target_branch_name, target_branch_name  # remote, local_branch, remote_branch
    else:
        target_branch = get_xos_target_branch_for_tracked_project(mapping.local_path)
        return "XOS", target_branch, target_branch  # remote, local_branch, remote_branch


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

    conflict_result = check_conflict_state(cherry_picker, mapping, patch_ref, patch_url, dry_run)
    if conflict_result:
        return conflict_result

    if not project_path.exists():
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "directory not found")

    try:
        with GitRepoLock(project_path):
            repo = git.Repo(project_path)

            merge_conflict_result = check_merge_conflicts(repo, project_path, mapping, patch_ref, patch_url)
            if merge_conflict_result:
                return merge_conflict_result

            if dry_run:
                return create_dry_run_result(mapping, patch_ref, patch_url, project_path, onto, security_patch_level, branch_name)

            try:
                aosp_remote = setup_repository(repo, mapping, project_path)
            except Exception as e:
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, str(e))

            used_existing_branch = False
            if onto and (security_patch_level or branch_name):
                onto_ref, error_result = setup_onto_branch(repo, onto, mapping, patch_ref, patch_url)
                if error_result:
                    return error_result
            else:
                error_result = setup_default_branch(repo, mapping, patch_ref, patch_url)
                if error_result:
                    return error_result

            try:
                if progress and bucket_task:
                    progress.update(bucket_task, description=f"[yellow]Fetching {mapping.local_path} ({patch_ref[:12]})")

                aosp_remote = safe_get_remote(repo, 'aosp')
                if not aosp_remote:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "aosp remote not found")
                aosp_remote.fetch()

                if progress and bucket_task:
                    progress.update(bucket_task, description=f"[green]Cherry-picking {mapping.local_path} ({patch_ref[:12]})")

                # 1. Check if already handled (exists between upstream..HEAD or [ALREADY APPLIED])
                already_result = check_already_handled(repo, patch_ref, mapping, patch_url, onto_ref if onto else None, cherry_picker)
                if already_result:
                    return already_result

                # 2. Check if commit exists in upstream → ALREADY APPLIED (only if we have an upstream ref)
                upstream_ref = find_upstream_ref(repo, onto_ref, mapping) if onto else None
                existing_commit = find_similar_commit_by_message(repo, patch_ref, similarity_threshold=0.9, until_ref=upstream_ref) if upstream_ref else None
                if existing_commit:
                    cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: Found similar commit in upstream: {existing_commit[:8]} for {patch_ref[:8]}")
                    return create_already_applied_commit(repo, patch_ref, existing_commit, cherry_picker, mapping, patch_url)
                else:
                    cherry_picker.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: No similar commit found for {patch_ref[:8]} (fuzzy matching)")

                # 3. Try the cherry-pick
                try:
                    repo.git.cherry_pick(patch_ref, '--no-edit')
                except git.exc.GitCommandError as cherry_pick_error:
                    error_msg = str(cherry_pick_error)
                    is_empty_cherrypick = ("nothing to commit" in error_msg.lower() or "no changes added to commit" in error_msg.lower() or "would result in an empty commit" in error_msg.lower() or "the previous cherry-pick is now empty" in error_msg.lower())

                    if is_empty_cherrypick:
                        return create_no_change_commit(repo, patch_ref, cherry_picker, mapping, patch_url)
                    else:
                        raise cherry_pick_error

            except git.exc.GitCommandError as e:
                error_result = handle_cherry_pick_error(str(e), repo, patch_ref, cherry_picker, mapping, patch_url)
                if error_result:
                    return error_result

            had_lfs, lfs_error_result = handle_lfs_cleanup_if_needed(project_path, mapping, patch_ref, patch_url, dry_run)
            if lfs_error_result:
                return lfs_error_result

            remote_name, local_branch, remote_branch = get_push_parameters(onto, security_patch_level, branch_name, mapping)

            return CherryPickResult(
                mapping.local_path,
                patch_ref,
                patch_url,
                True,
                "cherry-pick completed successfully",
                needs_push=True,
                push_remote=remote_name,
                push_local_branch=local_branch,
                push_remote_branch=remote_branch,
                had_lfs=had_lfs,
                used_existing_branch=used_existing_branch
            )

    except Exception as e:
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"unexpected error: {str(e)}")


class BulletinCherryPicker:
    def __init__(self, bulletin_dates: List[str], android_version: str, dry_run: bool = False, max_workers: int = 4, push_only: bool = False, onto: Optional[str] = None, branch_name: Optional[str] = None, force_recreate: bool = False, verbose: bool = False, directory_filter: Optional[str] = None):
        self.bulletin_dates = bulletin_dates
        self.android_version = android_version
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.push_only = push_only
        self.onto = onto
        self.branch_name = branch_name
        self.force_recreate = force_recreate
        self.verbose = verbose
        self.directory_filter = directory_filter
        self.top = get_android_top()
        self.conflicted_repos = set()  # Track repos with conflicts
        self.conflict_lock = threading.Lock()  # Thread-safe access to conflicted_repos

    def verbose_print(self, message):
        """Print message only if verbose mode is enabled."""
        if self.verbose:
            console.print(message)

    def get_unique_repos_from_tasks(self, tasks: List[CherryPickTask]):
        """Extract unique repository paths from tasks."""
        unique_repos = set()
        for task in tasks:
            unique_repos.add(task.project_mapping.local_path)
        return unique_repos

    def determine_checkout_ref(self, repo, onto_ref):
        """Determine the appropriate checkout reference (tag or remote/ref)."""
        try:
            repo.git.show_ref('--tags', f'refs/tags/{onto_ref}')
            return onto_ref  # It's a tag
        except git.exc.GitCommandError:
            return self.onto  # Use remote/ref format

    def find_remote_ref_for_bare_branch(self, repo, branch_name):
        """Find a remote ref for a bare branch name."""
        for remote in repo.remotes:
            try:
                remote.fetch(refspec=branch_name)
                return f"{remote.name}/{branch_name}"
            except git.exc.GitCommandError:
                continue
        return None

    def setup_single_repository_branch(self, repo_path, onto_ref, bare_branch=False, mapping=None):
        """Setup branch in a single repository."""
        try:
            import git
            from pathlib import Path
            project_path = Path(self.top) / repo_path
            if not project_path.exists():
                return

            repo = git.Repo(project_path)

            # Check existing branch first — preserve work from previous runs
            existing_branch = None
            for branch in repo.branches:
                if branch.name == self.branch_name:
                    existing_branch = branch
                    break

            current_branch = None
            try:
                current_branch = repo.active_branch.name
            except TypeError:
                pass

            if existing_branch and not self.force_recreate:
                if current_branch != self.branch_name:
                    repo.git.checkout(self.branch_name)
                    self.verbose_print(f"  [yellow]→[/yellow] Checked out existing {repo_path}:{self.branch_name}")
                else:
                    self.verbose_print(f"  [yellow]→[/yellow] Already on {repo_path}:{self.branch_name}")
                return

            if bare_branch:
                remote_ref = self.find_remote_ref_for_bare_branch(repo, onto_ref)
                if not remote_ref:
                    success, msg = setup_tracked_project_branch(project_path, repo_path, onto_ref)
                    if success:
                        self.verbose_print(f"  [green]→[/green] {repo_path}: {msg}")
                    else:
                        self.verbose_print(f"  [red]→[/red] {repo_path}: {msg}")
                    return
                checkout_ref = remote_ref
            else:
                checkout_ref = self.determine_checkout_ref(repo, onto_ref)

            if existing_branch:
                # force_recreate is True here
                if current_branch != self.branch_name:
                    repo.git.checkout(self.branch_name)
                repo.git.reset('--hard', checkout_ref)
                self.verbose_print(f"  [yellow]→[/yellow] Reset {repo_path}:{self.branch_name} to {checkout_ref}")
            else:
                repo.git.checkout('-b', self.branch_name, checkout_ref)
                self.verbose_print(f"  [yellow]→[/yellow] Created {repo_path}:{self.branch_name} from {checkout_ref}")

        except Exception as e:
            self.verbose_print(f"  [red]→[/red] Failed to setup branch in {repo_path}: {str(e)}")

    def setup_branches(self, tasks: List[CherryPickTask], project_mappings: Dict[str, ProjectMapping]):
        """Setup branches in all repositories that will be cherry-picked to."""
        if not self.onto or not self.branch_name:
            return

        bare_branch = '/' not in self.onto
        if bare_branch:
            onto_ref = self.onto
        else:
            onto_remote, onto_ref = self.onto.split('/', 1)

        unique_repos = self.get_unique_repos_from_tasks(tasks)

        if self.force_recreate:
            console.print(f"[cyan]Recreating branch '{self.branch_name}' in {len(unique_repos)} repositories...[/cyan]")
        else:
            console.print(f"[cyan]Setting up branches in {len(unique_repos)} repositories...[/cyan]")

        # Build mapping lookup for bare branch setup
        mapping_lookup = {}
        for task in tasks:
            mapping_lookup[task.project_mapping.local_path] = task.project_mapping

        for repo_path in unique_repos:
            self.setup_single_repository_branch(repo_path, onto_ref, bare_branch=bare_branch, mapping=mapping_lookup.get(repo_path))


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
                # Apply directory filter if specified
                if self.directory_filter and mapping.local_path != self.directory_filter:
                    continue

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

    def get_pushable_picks(self, successful_picks: List[CherryPickResult]):
        """Filter results that need pushing."""
        return [pick for pick in successful_picks if pick.needs_push and pick.push_remote and pick.push_local_branch and pick.push_remote_branch]

    def get_xos_remote_url(self, manifest_path: Optional[Path]):
        """Extract XOS remote URL from manifest."""
        if not manifest_path:
            return None

        try:
            manifest = ManifestParser(manifest_path)
            xos_remote_config = manifest.get_remote_config("XOS")
            if xos_remote_config and xos_remote_config.get('fetch'):
                return xos_remote_config['fetch']
        except Exception as e:
            console.print(f"[yellow]Warning: Could not parse manifest for remote info: {e}[/yellow]")
        return None

    def convert_https_to_ssh_url(self, https_url: str) -> str:
        """Convert HTTPS Git URL to SSH format for pushing."""
        if not https_url.startswith("https://"):
            return https_url

        # Parse https://domain.com/group -> git@domain.com:group
        import re
        match = re.match(r'https://([^/]+)/(.+)', https_url)
        if match:
            domain = match.group(1)
            path = match.group(2)
            return f"git@{domain}:{path}"

        return https_url

    def ensure_remote_repository_exists(self, result, xos_remote_url):
        """Create remote repository if it doesn't exist."""
        remote_name = result.push_remote or "XOS"

        if xos_remote_url and remote_name == "XOS":
            project_path = self.top / result.project_path
            repo_name = get_project_path(result.project_path)
            remote_repo_url = f"{xos_remote_url}/{repo_name}"

            if not check_if_xos_repo_exists(remote_repo_url, project_path):
                console.print(f"[yellow]Repository {repo_name} does not exist, creating...[/yellow]")
                if not create_xos_repo(repo_name):
                    return False, "Failed to create repository"
                console.print(f"[green]✓[/green] Created repository {repo_name}")
        return True, None

    def execute_single_push(self, result):
        """Execute push for a single repository using GitPython."""
        try:
            project_path = self.top / result.project_path
            repo = git.Repo(project_path)

            # Get the remote
            remote = repo.remotes[result.push_remote]

            # Push using GitPython
            push_info = remote.push(f"{result.push_local_branch}:{result.push_remote_branch}")

            # Check if push was successful
            for info in push_info:
                if info.flags & info.ERROR:
                    console.print(f"[red]✗[/red] {result.project_path}: Push failed: {info.summary}")
                    return False, info.summary
                elif info.flags & info.REJECTED:
                    console.print(f"[red]✗[/red] {result.project_path}: Push rejected: {info.summary}")
                    return False, info.summary

            console.print(f"[green]✓[/green] {result.project_path}: Pushed successfully")
            return True, None

        except Exception as e:
            console.print(f"[red]✗[/red] {result.project_path}: Push exception: {e}")
            return False, str(e)

    def execute_single_push_with_progress(self, result, progress, push_task, xos_remote_url=None):
        """Execute push for a single repository with progress updates and git visibility."""
        try:
            # First ensure remote repository exists if needed
            if xos_remote_url:
                repo_created, create_error = self.ensure_remote_repository_exists(result, xos_remote_url)
                if not repo_created:
                    console.print(f"[red]✗[/red] {result.project_path}: {create_error}")
                    return False, create_error

            project_path = self.top / result.project_path
            repo = git.Repo(project_path)

            # Update progress to show current operation
            short_path = truncate_project_name(result.project_path)
            progress.update(push_task, description=f"[yellow]Pushing {short_path} to {result.push_remote}")

            # Get the remote, handle missing remote
            try:
                remote = repo.remotes[result.push_remote]
            except IndexError:
                # If XOS remote is missing and we have the base URL, try to add it
                if result.push_remote == "XOS" and xos_remote_url:
                    try:
                        repo_name = get_project_path(result.project_path)
                        # Convert HTTPS URL to SSH URL for pushing
                        ssh_base_url = self.convert_https_to_ssh_url(xos_remote_url)
                        xos_url = f"{ssh_base_url}/{repo_name}"
                        remote = safe_add_or_update_remote(repo, "XOS", xos_url)
                        console.print(f"[blue]→[/blue] {result.project_path}: Added XOS remote {xos_url}")
                    except Exception as e:
                        console.print(f"[red]✗[/red] {result.project_path}: Failed to add XOS remote: {e}")
                        return False, f"Failed to add XOS remote: {e}"
                else:
                    console.print(f"[red]✗[/red] {result.project_path}: Remote '{result.push_remote}' not found")
                    return False, f"Remote '{result.push_remote}' not configured in repository"

            # Push using GitPython with progress callback
            push_ref = f"{result.push_local_branch}:{result.push_remote_branch}"

            console.print(f"  [blue]→[/blue] {result.project_path}: git push {result.push_remote} {push_ref}")

            # Custom progress handler to show git transfer progress
            class PushProgressHandler:
                def __init__(self, project_path):
                    self.project_path = project_path

                def __call__(self, op_code, cur_count, max_count=None, message=''):
                    if message:
                        # Show git's native progress messages (upload rate, objects, etc.)
                        console.print(f"    [dim]{self.project_path}:[/dim] {message}")

            progress_handler = PushProgressHandler(result.project_path)

            # Get the actual push URL from git config to ensure SSH is used
            try:
                push_url = repo.git.config('--get', f'remote.{result.push_remote}.pushurl')
            except git.exc.GitCommandError:
                # No separate push URL, use the fetch URL
                push_url = remote.url

            # Configure git to fail immediately if authentication is required
            # This prevents hanging on password prompts
            with repo.git.custom_environment(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true'):
                try:
                    # Use git command directly with explicit URL to ensure proper SSH usage
                    push_output = repo.git.push(push_url, push_ref, '--progress', with_extended_output=True)
                    console.print(f"[green]✓[/green] {result.project_path}: Pushed successfully")

                    # Show git's progress output (stderr contains the progress info)
                    if hasattr(push_output, 'stderr') and push_output.stderr:
                        for line in push_output.stderr.split('\n'):
                            if line.strip() and ('Enumerating' in line or 'Counting' in line or 'Writing' in line or 'Total' in line):
                                console.print(f"    [dim]{result.project_path}:[/dim] {line}")

                    return True, None
                except git.exc.GitCommandError as e:
                    error_msg = str(e)
                    console.print(f"[red]✗[/red] {result.project_path}: Push failed: {error_msg}")
                    return False, error_msg

        except Exception as e:
            console.print(f"[red]✗[/red] {result.project_path}: Push exception: {e}")
            return False, str(e)
        finally:
            # Update progress bar
            progress.update(push_task, advance=1)

    def execute_pushes(self, successful_picks: List[CherryPickResult], manifest_path: Optional[Path] = None) -> Tuple[int, List[Tuple[str, str]]]:
        """Execute all push operations at the end."""
        pushable_picks = self.get_pushable_picks(successful_picks)

        if self.dry_run:
            console.print(f"[blue][DRY RUN] Would push {len(pushable_picks)} repositories[/blue]")
            return len(pushable_picks), []

        if not pushable_picks:
            console.print("[blue]No repositories need pushing (all patches were already applied)[/blue]")
            return 0, []

        xos_remote_url = self.get_xos_remote_url(manifest_path)
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

            # Use ThreadPoolExecutor for concurrent pushes
            max_push_workers = min(self.max_workers, len(pushable_picks))
            with ThreadPoolExecutor(max_workers=max_push_workers) as executor:
                # Submit all push tasks
                future_to_result = {}
                for result in pushable_picks:
                    # Submit push task with repository creation handled in worker
                    future = executor.submit(self.execute_single_push_with_progress, result, progress, push_task, xos_remote_url)
                    future_to_result[future] = result

                # Process completed pushes
                for future in as_completed(future_to_result):
                    result = future_to_result[future]
                    try:
                        success, error = future.result()
                        if success:
                            push_successes += 1
                        else:
                            push_failures.append((result.project_path, error))
                    except Exception as e:
                        push_failures.append((result.project_path, f"Push exception: {str(e)}"))

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

    def create_success_table(self, successful_picks: List[CherryPickResult]):
        """Create and populate table for successful cherry-picks."""
        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("Patch", style="yellow", no_wrap=True)
        table.add_column("Message", style="green")
        table.add_column("Branch", style="magenta", no_wrap=True)
        table.add_column("", width=2)

        for result in successful_picks:
            emoji = "✅" if result.needs_push else "⏭️"
            branch_status = "existing" if result.used_existing_branch else "new"
            table.add_row(
                truncate_project_name(result.project_path),
                result.patch_ref[:12],
                result.message,
                branch_status,
                emoji
            )
        return table

    def create_failure_table(self, failed_picks: List[CherryPickResult]):
        """Create and populate table for failed cherry-picks."""
        fail_table = Table(show_header=True, header_style="bold red")
        fail_table.add_column("Project", style="cyan", no_wrap=True)
        fail_table.add_column("Patch", style="yellow", no_wrap=True)
        fail_table.add_column("Error", style="red")
        fail_table.add_column("", width=2)

        for result in failed_picks:
            fail_table.add_row(
                truncate_project_name(result.project_path),
                result.patch_ref[:12],
                result.message,
                "❌"
            )
        return fail_table

    def display_cherry_pick_results(self, successful_picks: List[CherryPickResult], failed_picks: List[CherryPickResult]):
        """Display cherry-pick results in a formatted table."""
        if not successful_picks and not failed_picks:
            return

        if successful_picks:
            console.print(f"\n[bold green]Successful cherry-picks ({len(successful_picks)}):[/bold green]")
            success_table = self.create_success_table(successful_picks)
            console.print(success_table)

        if failed_picks:
            console.print(f"\n[bold red]Failed cherry-picks ({len(failed_picks)}):[/bold red]")
            failure_table = self.create_failure_table(failed_picks)
            console.print(failure_table)

    def fetch_all_bulletin_data(self):
        """Fetch bulletin data from all specified dates."""
        all_bulletin_data = []
        all_patches = []

        for bulletin_date in self.bulletin_dates:
            console.print(f"[cyan]Fetching security bulletin for {bulletin_date}...[/cyan]")
            bulletin_data = get_bulletin_patches(bulletin_date)
            all_bulletin_data.extend(bulletin_data)

            console.print(f"[cyan]Filtering patches for Android {self.android_version} from {bulletin_date}...[/cyan]")
            patches = filter_patches_by_android_version(bulletin_data, self.android_version)
            all_patches.extend(patches)

            if patches:
                console.print(f"[green]Found {len(patches)} patches for Android {self.android_version} in {bulletin_date}[/green]")
            else:
                console.print(f"[yellow]No patches found for Android {self.android_version} in {bulletin_date}[/yellow]")

        if not all_patches:
            console.print(f"[yellow]No patches found for Android {self.android_version} in any of the specified bulletins[/yellow]")
            return None, None

        console.print(f"[green]Total patches found: {len(all_patches)} across {len(self.bulletin_dates)} bulletins[/green]")
        console.print(f"[green]Processing {len(all_patches)} patches for Android {self.android_version}[/green]")

        return all_bulletin_data, all_patches

    def prepare_cherry_pick_tasks(self, bulletin_data, patches):
        """Generate manifest and create cherry-pick tasks."""
        self.verbose_print("[cyan]Generating temporary manifest file...[/cyan]")
        manifest_path = self.generate_manifest()

        self.verbose_print("[cyan]Building project mappings from manifest...[/cyan]")
        project_mappings = build_project_mappings(manifest_path)
        self.verbose_print(f"[green]Built {len(project_mappings)} project mappings[/green]")

        self.verbose_print("[cyan]Creating cherry-pick tasks...[/cyan]")
        tasks = self.create_cherry_pick_tasks(bulletin_data, patches, project_mappings)
        self.verbose_print(f"[green]Created {len(tasks)} cherry-pick tasks[/green]")

        if self.onto and self.branch_name:
            if self.force_recreate:
                console.print(f"[cyan]Force recreating branches in all repositories...[/cyan]")
            else:
                console.print(f"[cyan]Setting up branches in all repositories...[/cyan]")
            self.setup_branches(tasks, project_mappings)

        return manifest_path, tasks

    def execute_cherry_picks_or_push_only(self, tasks):
        """Execute cherry-picks or handle push-only mode."""
        if self.push_only:
            console.print(f"[blue]Push-only mode: Creating push tasks for {len(tasks)} repositories[/blue]")
            pushable_results = []
            unique_repos = {}

            # Get unique repositories from tasks
            for task in tasks:
                repo_path = task.project_mapping.local_path
                if repo_path not in unique_repos:
                    unique_repos[repo_path] = task

            # Create push results for all repositories
            for repo_path, task in unique_repos.items():
                mapping = task.project_mapping
                remote_name, local_branch, remote_branch = get_push_parameters(task.onto, task.security_patch_level, task.branch_name, mapping)
                result = CherryPickResult(
                    repo_path,
                    "push-only",
                    "",
                    True,
                    "ready for push",
                    needs_push=True,
                    push_remote=remote_name,
                    push_local_branch=local_branch,
                    push_remote_branch=remote_branch
                )
                pushable_results.append(result)

            return pushable_results, []
        else:
            return self.process_cherry_picks(tasks)

    def handle_pushes(self, successful_picks, manifest_path):
        """Handle push operations if needed."""
        push_successes = 0
        push_failures = []

        if successful_picks and not self.dry_run and self.push_only:
            push_successes, push_failures = self.execute_pushes(successful_picks, manifest_path)

        return push_successes, push_failures

    def print_summary(self, successful_picks, failed_picks, push_successes, push_failures, tasks):
        """Print final summary of operations."""
        console.print("\n" + "=" * 60)
        console.print("[bold]Security bulletin cherry-pick complete![/bold]")
        console.print(f"[green]Successfully cherry-picked:[/green] {len(successful_picks)}/{len(tasks)} patches")

        if not self.dry_run and successful_picks:
            console.print(f"[green]Successfully pushed:[/green] {push_successes}/{len([p for p in successful_picks if p.needs_push])} repositories")

        if failed_picks:
            console.print(f"\n[red]Failed cherry-picks ({len(failed_picks)}):[/red]")
            for result in failed_picks:
                if not result.verbose_only or self.verbose:
                    console.print(f"  [red]- {result.project_path} ({result.patch_ref[:12]}):[/red] {result.message}")

        if push_failures:
            console.print(f"\n[red]Failed pushes ({len(push_failures)}):[/red]")
            for project_path, error in push_failures:
                console.print(f"  [red]- {project_path}:[/red] {error}")


    def run(self):
        """Main execution flow."""
        try:
            bulletin_data, patches = self.fetch_all_bulletin_data()
            if not patches:
                return 0
        except Exception as e:
            console.print(f"[red]Failed to fetch bulletin data: {e}[/red]")
            return 1

        try:
            manifest_path, tasks = self.prepare_cherry_pick_tasks(bulletin_data, patches)
        except Exception as e:
            console.print(f"[red]Failed to prepare cherry-pick tasks: {e}[/red]")
            return 1

        successful_picks, failed_picks = self.execute_cherry_picks_or_push_only(tasks)
        self.display_cherry_pick_results(successful_picks, failed_picks)

        push_successes, push_failures = self.handle_pushes(successful_picks, manifest_path)
        cleanup_manifest(manifest_path, self.dry_run)

        self.print_summary(successful_picks, failed_picks, push_successes, push_failures, tasks)

        console.print("\n[bold green]Everything done.[/bold green]")
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

    parser.add_argument(
        "--directory",
        type=str,
        help="Only process patches for the specified directory (exact match)"
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
            verbose=args.verbose,
            directory_filter=args.directory
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