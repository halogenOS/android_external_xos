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


def perform_single_cherry_pick(task: CherryPickTask, dry_run: bool, cherry_picker: 'BulletinCherryPicker' = None) -> CherryPickResult:
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
                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "skipped due to previous conflict in this repository")

    if not project_path.exists():
        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "directory not found")

    try:
        # Acquire repository lock to prevent concurrent git operations
        with GitRepoLock(project_path):
            repo = git.Repo(project_path)

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
                # Parse onto parameter for remote/ref format
                onto_remote = None
                onto_ref = onto
                if '/' in onto:
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

                        # Fetch from the remote to ensure we have the ref
                        remote_obj.fetch()
                    except Exception as e:
                        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to fetch from remote '{onto_remote}': {str(e)}")

                # Use custom branch name if provided, otherwise generate ASB branch name
                base_onto_ref = onto_ref if onto_remote else onto
                target_branch_name = branch_name if branch_name else f"{base_onto_ref}-ASB-{security_patch_level}"

                try:
                    current_branch = repo.active_branch.name
                except TypeError:
                    current_branch = None

                if current_branch != target_branch_name:
                    try:
                        # Check if target branch already exists
                        existing_branch = None
                        for branch in repo.heads:
                            if branch.name == target_branch_name:
                                existing_branch = branch
                                break

                        if existing_branch:
                            # Branch exists, just checkout (no reset)
                            repo.git.checkout(target_branch_name)
                            used_existing_branch = True
                        else:
                            # Create new branch based on the onto ref (with remote if specified)
                            try:
                                full_onto_ref = f"{onto_remote}/{onto_ref}" if onto_remote else onto
                                repo.git.checkout('-b', target_branch_name, full_onto_ref)
                            except git.exc.GitCommandError as e:
                                return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to create branch {target_branch_name} from {full_onto_ref}: {str(e)}")

                    except git.exc.GitCommandError as e:
                        return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to checkout/create branch {target_branch_name}: {str(e)}")
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
                # Fetch from AOSP to get the commit
                aosp_remote = safe_get_remote(repo, 'aosp')
                if not aosp_remote:
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, "aosp remote not found")
                aosp_remote.fetch()

                # Check if commit already exists in current branch
                try:
                    # Check if the commit is already in the current branch history
                    repo.git.merge_base('--is-ancestor', patch_ref, 'HEAD')
                    return CherryPickResult(mapping.local_path, patch_ref, patch_url, True, "patch already applied (skipped)", needs_push=False)
                except git.exc.GitCommandError:
                    # Commit is not in current branch, proceed with cherry-pick
                    pass

                # Use fuzzy matching to find similar commits (>90% similarity)
                existing_commit = find_similar_commit_by_message(repo, patch_ref, similarity_threshold=0.9)

                # Try cherry-pick
                try:
                    repo.git.cherry_pick(patch_ref, '--no-edit')
                except git.exc.GitCommandError as cherry_pick_error:
                    error_msg = str(cherry_pick_error)

                    # Check if it's just empty changes and we have existing commit
                    if existing_commit and ("nothing to commit" in error_msg.lower() or "no changes added to commit" in error_msg.lower() or "would result in an empty commit" in error_msg.lower()):
                        # Abort the cherry-pick and clean up
                        try:
                            repo.git.cherry_pick('--abort')
                            repo.git.reset('--hard', 'HEAD')
                        except:
                            pass

                        # Create empty commit with [ALREADY APPLIED] prefix
                        try:
                            # Get the original commit message for the [ALREADY APPLIED] commit
                            original_commit = repo.commit(patch_ref)
                            patch_title = original_commit.message.split('\n')[0].strip()
                            already_applied_msg = f"[ALREADY APPLIED] {patch_title}\n\nOriginal commit was already applied in {existing_commit}\nCherry-picked from: {patch_ref}"
                            repo.git.commit('--allow-empty', '-m', already_applied_msg)
                        except git.exc.GitCommandError as commit_error:
                            return CherryPickResult(mapping.local_path, patch_ref, patch_url, False, f"failed to create [ALREADY APPLIED] commit: {str(commit_error)}")
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
    def __init__(self, bulletin_dates: List[str], android_version: str, dry_run: bool = False, max_workers: int = 4, push_only: bool = False, onto: Optional[str] = None, branch_name: Optional[str] = None):
        self.bulletin_dates = bulletin_dates
        self.android_version = android_version
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.push_only = push_only
        self.onto = onto
        self.branch_name = branch_name
        self.top = get_android_top()
        self.conflicted_repos = set()  # Track repos with conflicts
        self.conflict_lock = threading.Lock()  # Thread-safe access to conflicted_repos

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
            console.print(f"[yellow]Warning: No local projects found for {len(unmatched_patches)} patches:[/yellow]")
            for repo_name in unmatched_patches[:5]:  # Show first 5
                console.print(f"  [yellow]- {repo_name}[/yellow]")
            if len(unmatched_patches) > 5:
                console.print(f"  [yellow]... and {len(unmatched_patches) - 5} more[/yellow]")

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
        console.print(f"\n[bold]{dry_run_prefix}Processing {len(tasks)} cherry-pick operations...[/bold]")

        successful_picks = []
        failed_picks = []

        # Process with progress bar and threading
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:

            pick_task = progress.add_task(
                f"[cyan]Cherry-picking patches {dry_run_prefix.strip()}",
                total=len(tasks)
            )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor_pool:
                executor = executor_pool

                # Submit all tasks
                futures = {
                    executor_pool.submit(perform_single_cherry_pick, task, self.dry_run, self): task
                    for task in tasks
                }

                # Process results as they complete
                for future in as_completed(futures):
                    if stop_event.is_set():
                        executor_pool.shutdown(wait=False, cancel_futures=True)
                        break

                    try:
                        task = futures[future]
                        result = future.result()

                        if result.success:
                            successful_picks.append(result)
                        else:
                            failed_picks.append(result)

                        progress.update(pick_task, advance=1)

                    except Exception as e:
                        task = futures[future]
                        failed_result = CherryPickResult(task.project_mapping.local_path, task.patch_info['ref'], task.patch_info['url'], False, f"exception: {str(e)}")
                        failed_picks.append(failed_result)
                        progress.update(pick_task, advance=1)

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
            console.print("[cyan]Generating temporary manifest file...[/cyan]")
            manifest_path = self.generate_manifest()

            console.print("[cyan]Building project mappings from manifest...[/cyan]")
            project_mappings = build_project_mappings(manifest_path)
            console.print(f"[green]Built {len(project_mappings)} project mappings[/green]")

            console.print("[cyan]Creating cherry-pick tasks...[/cyan]")
            tasks = self.create_cherry_pick_tasks(bulletin_data, patches, project_mappings)
            console.print(f"[green]Created {len(tasks)} cherry-pick tasks[/green]")

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

        # Execute pushes for successful cherry-picks (deferred pushing)
        push_successes = 0
        push_failures = []

        # Block pushing if any cherry-pick failed (unless in push-only mode)
        if successful_picks and not self.dry_run:
            if failed_picks and not self.push_only:
                console.print(f"\n[yellow]⚠️  Skipping push because {len(failed_picks)} cherry-picks failed.[/yellow]")
                console.print("[yellow]Use --push-only flag to push successful cherry-picks after fixing failures.[/yellow]")
            else:
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
            for result in failed_picks[:5]:  # Show first 5
                console.print(f"  [red]- {result.project_path} ({result.patch_ref[:12]}):[/red] {result.message}")
            if len(failed_picks) > 5:
                console.print(f"  [red]... and {len(failed_picks) - 5} more failures[/red]")

        if push_failures:
            console.print(f"\n[red]Failed pushes ({len(push_failures)}):[/red]")
            for project_path, error in push_failures:
                console.print(f"  [red]- {project_path}:[/red] {error}")

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
  %(prog)s 2025-09-01 --android-version 15 --onto android-16.0.0_r1  # Auto-generated ASB branches
  %(prog)s 2025-07-01 2025-08-01 --android-version 16 --onto android-16.0.0_r1 --branch-name android-16.0.0_r1-ASB_2025-08-01  # Custom branch name
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
        help="Create a new branch '<onto>-ASB-<security_patch_level>' and cherry-pick onto it"
    )

    parser.add_argument(
        "--branch-name",
        type=str,
        help="Custom branch name to use instead of auto-generated ASB branch name"
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
            branch_name=args.branch_name
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