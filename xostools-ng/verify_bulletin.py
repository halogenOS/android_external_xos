#!/usr/bin/env python3
"""
Verify cherry-picked security patches from Android Security Bulletins.

Verifies that security patches for a specific Android version from
Android Security Bulletins have been successfully applied to the current branch.
"""

import os
import sys
import signal
import argparse
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
    get_project_path
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
class VerificationTask:
    """A single verification task."""
    project_mapping: ProjectMapping
    patch_info: Dict[str, Any]

@dataclass
class VerificationResult:
    """Result of a verification operation."""
    project_path: str
    patch_ref: str
    patch_url: str
    verified: bool
    message: str
    applied_commit: Optional[str] = None  # The actual applied commit if found
    method: Optional[str] = None  # How the patch was found (direct, similar)
    patch_info: Optional[Dict[str, Any]] = None  # Original patch information including CVE data


def setup_progress_bar(total_tasks: int, console):
    """Setup and return progress bar with main progress bar."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console
    )

    # Main progress bar
    verify_task = progress.add_task(
        f"[cyan]Verifying patches",
        total=total_tasks
    )

    return progress, verify_task


def group_tasks_by_repository(tasks):
    """Group tasks by repository to ensure serial execution within each repo."""
    repo_buckets = {}
    for task in tasks:
        repo_path = task.project_mapping.local_path
        if repo_path not in repo_buckets:
            repo_buckets[repo_path] = []
        repo_buckets[repo_path].append(task)
    return repo_buckets


def process_repo_bucket(repo_path, repo_tasks, verifier, progress, verify_task, stop_event):
    """Process all verification tasks for a single repository sequentially."""
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
            result = perform_single_verification(task, verifier, progress, bucket_task)
            bucket_results.append(result)
        except Exception as e:
            failed_result = VerificationResult(
                task.project_mapping.local_path,
                task.patch_info['ref'],
                task.patch_info['url'],
                False,
                f"exception: {str(e)}",
                patch_info=task.patch_info
            )
            bucket_results.append(failed_result)

        # Update both main progress and bucket progress
        progress.update(verify_task, advance=1)
        progress.update(bucket_task, advance=1)

    # Remove the bucket progress bar when done
    progress.remove_task(bucket_task)

    return bucket_results


def submit_verification_buckets(executor_pool, repo_buckets, verifier, progress, verify_task, stop_event):
    """Submit repository buckets to the executor pool."""
    return {
        executor_pool.submit(process_repo_bucket, repo_path, repo_tasks, verifier, progress, verify_task, stop_event): repo_path
        for repo_path, repo_tasks in repo_buckets.items()
    }


def process_completed_bucket_futures(futures, progress, stop_event, verified_patches, unverified_patches, console):
    """Process completed bucket futures and update results."""
    for future in as_completed(futures):
        if stop_event.is_set():
            break

        try:
            repo_path = futures[future]
            bucket_results = future.result()

            # Process all results from this bucket
            for result in bucket_results:
                if result.verified:
                    verified_patches.append(result)
                else:
                    unverified_patches.append(result)

        except Exception as e:
            repo_path = futures[future]
            # Handle bucket-level exceptions - could affect multiple tasks
            console.print(f"[red]Exception processing repository bucket {repo_path}: {str(e)}[/red]")


def get_commit_author_date(repo, commit_ref):
    """Get the author date of a commit."""
    try:
        commit = repo.commit(commit_ref)
        return commit.authored_datetime
    except Exception:
        return None


def perform_single_verification(task: VerificationTask, verifier: 'BulletinVerifier' = None, progress=None, bucket_task=None) -> VerificationResult:
    """Perform verification for a single patch."""
    mapping = task.project_mapping
    patch_info = task.patch_info
    patch_ref = patch_info['ref']
    patch_url = patch_info['url']

    project_path = get_android_top() / mapping.local_path

    if not project_path.exists():
        return VerificationResult(mapping.local_path, patch_ref, patch_url, False, "directory not found", patch_info=patch_info)

    try:
        # Acquire repository lock to prevent concurrent git operations
        with GitRepoLock(project_path):
            repo = git.Repo(project_path)

            # Update bucket progress to show fetching
            if progress and bucket_task:
                progress.update(bucket_task, description=f"[yellow]Checking {mapping.local_path} ({patch_ref[:12]})")

            # Add/update AOSP remote
            aosp_remote = ensure_aosp_remote(repo, mapping.aosp_name)

            # Get the author date of the original commit for --until optimization
            original_author_date = get_commit_author_date(repo, patch_ref)
            if original_author_date:
                # Subtract 1 second from author date as requested
                until_date = original_author_date - timedelta(seconds=1)
                until_arg = until_date.strftime('%Y-%m-%d %H:%M:%S')
            else:
                until_arg = None

            # Fetch from AOSP with --until optimization to save time
            try:
                aosp_remote = safe_get_remote(repo, 'aosp')
                if not aosp_remote:
                    return VerificationResult(mapping.local_path, patch_ref, patch_url, False, "aosp remote not found", patch_info=patch_info)

                if until_arg:
                    try:
                        # Use git command directly to pass --until
                        repo.git.fetch('aosp', f'+refs/heads/*:refs/remotes/aosp/*', until=until_arg)
                    except git.exc.GitCommandError:
                        # Fallback to normal fetch if --until fails
                        aosp_remote.fetch()
                else:
                    aosp_remote.fetch()

            except git.exc.GitCommandError as e:
                return VerificationResult(mapping.local_path, patch_ref, patch_url, False, f"failed to fetch from aosp: {str(e)}")

            # Update bucket progress to show verification
            if progress and bucket_task:
                progress.update(bucket_task, description=f"[green]Verifying {mapping.local_path} ({patch_ref[:12]})")

            # Method 1: Check if commit is directly in current branch history
            try:
                repo.git.merge_base('--is-ancestor', patch_ref, 'HEAD')
                return VerificationResult(
                    mapping.local_path,
                    patch_ref,
                    patch_url,
                    True,
                    "patch directly applied",
                    applied_commit=patch_ref,
                    method="direct",
                    patch_info=patch_info
                )
            except git.exc.GitCommandError:
                # Commit is not directly in current branch
                pass

            # Method 2: Use fuzzy matching to find similar commits (>90% similarity)
            existing_commit = find_similar_commit_by_message(repo, patch_ref, similarity_threshold=0.9)
            if existing_commit:
                verifier.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: Found similar commit: {existing_commit[:8]} for {patch_ref[:8]}")
                return VerificationResult(
                    mapping.local_path,
                    patch_ref,
                    patch_url,
                    True,
                    "similar commit found (fuzzy match)",
                    applied_commit=existing_commit,
                    method="similar",
                    patch_info=patch_info
                )
            else:
                verifier.verbose_print(f"  [yellow]→[/yellow] {mapping.local_path}: No similar commit found for {patch_ref[:8]} (fuzzy matching)")

            # Patch not found
            return VerificationResult(mapping.local_path, patch_ref, patch_url, False, "patch not found in current branch")

    except Exception as e:
        return VerificationResult(mapping.local_path, patch_ref, patch_url, False, f"unexpected error: {str(e)}")


class BulletinVerifier:
    def __init__(self, bulletin_dates: List[str], android_version: str, max_workers: int = 4, verbose: bool = False, update_security_patch_level: bool = False):
        self.bulletin_dates = bulletin_dates
        self.android_version = android_version
        self.max_workers = max_workers
        self.verbose = verbose
        self.update_security_patch_level = update_security_patch_level
        self.top = get_android_top()

    def verbose_print(self, message):
        """Print message only if verbose mode is enabled."""
        if self.verbose:
            console.print(message)

    def update_security_patch_level_and_commit(self, latest_bulletin_date: str, verified_patches: List[VerificationResult]) -> bool:
        """Update security patch level and create a git commit with CVE information."""
        import os
        from pathlib import Path

        custom_product_dir = os.getenv('CUSTOM_PRODUCT_DIR')
        if not custom_product_dir:
            console.print("[yellow]CUSTOM_PRODUCT_DIR not set, skipping security patch level update[/yellow]")
            return False

        custom_product_path = Path(custom_product_dir)

        # Check if we're in a git repository
        try:
            repo = git.Repo(custom_product_path)
        except git.exc.InvalidGitRepositoryError:
            console.print(f"[red]{custom_product_path} is not a git repository[/red]")
            return False

        # Find the release codename directory in product/halogenOS/release/flag_values/
        release_flag_values_dir = custom_product_path / "release/flag_values"
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

        # Read current security patch level if file exists
        current_patch_level = None
        if security_patch_file.exists():
            try:
                with open(security_patch_file, 'r') as f:
                    content = f.read()
                    # Extract current patch level
                    import re
                    match = re.search(r'string_value:\s*"([^"]+)"', content)
                    if match:
                        current_patch_level = match.group(1)
            except Exception as e:
                console.print(f"[yellow]Could not read current security patch level: {e}[/yellow]")

        if current_patch_level == latest_bulletin_date:
            console.print(f"[yellow]Security patch level is already {latest_bulletin_date}[/yellow]")
            return True

        # Content for the security patch file (following the format from build/release)
        content = f'''name: "RELEASE_PLATFORM_SECURITY_PATCH"
value {{
  string_value: "{latest_bulletin_date}"
}}
'''

        try:
            # Create directory if it doesn't exist
            security_patch_file.parent.mkdir(parents=True, exist_ok=True)

            # Write the security patch file
            with open(security_patch_file, 'w') as f:
                f.write(content)

            console.print(f"[green]Updated security patch level to {latest_bulletin_date} in {security_patch_file}[/green]")

            # Create git commit with CVE information
            relative_file_path = security_patch_file.relative_to(custom_product_path)

            # Build commit message similar to the LineageOS example
            commit_message = f"Bump Security String to {latest_bulletin_date}\n\n"

            # Group patches by CVE information
            cve_info = {}
            for result in verified_patches:
                patch_info = result.patch_info or {}
                cve = patch_info.get('cve')
                severity = patch_info.get('severity', 'Unknown')
                patch_type = patch_info.get('type', 'Unknown')

                if cve and cve.startswith('CVE-'):
                    if cve not in cve_info:
                        cve_info[cve] = {
                            'severity': severity,
                            'type': patch_type,
                            'patches': []
                        }
                    cve_info[cve]['patches'].append(result)

            if cve_info:
                commit_message += "Implemented:\n"
                for cve, info in sorted(cve_info.items()):
                    commit_message += f"* {cve} ({info['type']}) (Severity: {info['severity']})\n"
                commit_message += "\n"

            commit_message += f"Verified {len(verified_patches)} security patches for Android {self.android_version}"

            # Stage the file
            repo.git.add(str(relative_file_path))

            # Create commit
            repo.git.commit('-m', commit_message)

            console.print(f"[green]Created commit: Bump Security String to {latest_bulletin_date}[/green]")
            return True

        except Exception as e:
            console.print(f"[red]Failed to update security patch level: {e}[/red]")
            return False

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        return run_repo_command(command, self.top, False)

    def generate_manifest(self) -> Path:
        """Generate temporary manifest file."""
        return generate_manifest(self.top, False)

    def create_verification_tasks(self, bulletin_data: List[Dict], patches: List[Dict], project_mappings: Dict[str, ProjectMapping]) -> List[VerificationTask]:
        """Create verification tasks from filtered patches."""
        tasks = []
        unmatched_patches = []

        for patch in patches:
            patch_repo_name = patch.get('name', '')
            matching_projects = find_matching_projects(patch_repo_name, project_mappings)
            if not matching_projects:
                unmatched_patches.append(patch_repo_name)
                continue

            # Create task for each matching project (in case of multiple matches)
            for mapping in matching_projects:
                task = VerificationTask(
                    project_mapping=mapping,
                    patch_info=patch
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

    def process_verifications(self, tasks: List[VerificationTask]) -> Tuple[List[VerificationResult], List[VerificationResult]]:
        """Process all verification tasks using multi-threading."""
        global executor

        if not tasks:
            console.print("[yellow]No verification tasks to process[/yellow]")
            return [], []

        self.verbose_print(f"\n[bold]Processing {len(tasks)} verification operations...[/bold]")

        verified_patches = []
        unverified_patches = []

        # Setup progress bar
        progress, verify_task = setup_progress_bar(len(tasks), console)

        # Group tasks by repository
        repo_buckets = group_tasks_by_repository(tasks)

        # Process with progress bar and threading
        with progress:
            # Limit workers to number of repository buckets to avoid over-threading
            max_workers = min(self.max_workers, len(repo_buckets))
            with ThreadPoolExecutor(max_workers=max_workers) as executor_pool:
                # Submit repository buckets
                futures = submit_verification_buckets(executor_pool, repo_buckets, self, progress, verify_task, stop_event)

                # Process results as they complete
                process_completed_bucket_futures(futures, progress, stop_event, verified_patches, unverified_patches, console)

                # Shutdown if stopped
                if stop_event.is_set():
                    executor_pool.shutdown(wait=False, cancel_futures=True)

        return verified_patches, unverified_patches

    def display_verification_results(self, verified_patches: List[VerificationResult], unverified_patches: List[VerificationResult]):
        """Display verification results in a formatted table."""
        if not verified_patches and not unverified_patches:
            return

        # Display verified patches
        if verified_patches:
            console.print(f"\n[bold green]Verified patches ({len(verified_patches)}):[/bold green]")

            table = Table(show_header=True, header_style="bold blue")
            table.add_column("Project", style="cyan", no_wrap=True)
            table.add_column("Patch", style="yellow", no_wrap=True)
            table.add_column("Method", style="green")
            table.add_column("Applied Commit", style="magenta", no_wrap=True)
            table.add_column("", width=2)  # Status column for emoji

            for result in verified_patches:
                method_display = {
                    "direct": "Direct",
                    "similar": "Similar commit"
                }.get(result.method, result.method or "Unknown")

                applied_commit_display = result.applied_commit[:12] if result.applied_commit else "N/A"

                table.add_row(
                    truncate_project_name(result.project_path),
                    result.patch_ref[:12],  # Show first 12 chars of commit hash
                    method_display,
                    applied_commit_display,
                    "✅"
                )

            console.print(table)

        # Display unverified patches
        if unverified_patches:
            console.print(f"\n[bold red]Unverified patches ({len(unverified_patches)}):[/bold red]")

            fail_table = Table(show_header=True, header_style="bold red")
            fail_table.add_column("Project", style="cyan", no_wrap=True)
            fail_table.add_column("Patch", style="yellow", no_wrap=True)
            fail_table.add_column("Reason", style="red")
            fail_table.add_column("", width=2)  # Status column for emoji

            for result in unverified_patches:
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

            console.print(f"[green]Verifying {len(patches)} patches for Android {self.android_version}[/green]")

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

            self.verbose_print("[cyan]Creating verification tasks...[/cyan]")
            tasks = self.create_verification_tasks(bulletin_data, patches, project_mappings)
            self.verbose_print(f"[green]Created {len(tasks)} verification tasks[/green]")

        except Exception as e:
            console.print(f"[red]Failed to prepare verification tasks: {e}[/red]")
            return 1

        # Process verifications
        verified_patches, unverified_patches = self.process_verifications(tasks)

        # Display results
        self.display_verification_results(verified_patches, unverified_patches)

        # Clean up temporary manifest
        cleanup_manifest(manifest_path, False)

        # Summary
        console.print("\n" + "=" * 60)
        console.print("[bold]Security bulletin verification complete![/bold]")
        console.print(f"[green]Verified patches:[/green] {len(verified_patches)}/{len(tasks)}")

        if unverified_patches:
            console.print(f"[red]Unverified patches:[/red] {len(unverified_patches)}")

        # Update security patch level if requested and all patches are verified
        if self.update_security_patch_level:
            if unverified_patches:
                console.print(f"[red]Cannot update security patch level: {len(unverified_patches)} patches are unverified[/red]")
            else:
                # Find the latest bulletin date
                latest_bulletin_date = max(self.bulletin_dates)
                console.print(f"\n[cyan]All patches verified! Updating security patch level to {latest_bulletin_date}...[/cyan]")
                success = self.update_security_patch_level_and_commit(latest_bulletin_date, verified_patches)
                if success:
                    console.print("[green]Security patch level updated and committed successfully![/green]")
                else:
                    console.print("[red]Failed to update security patch level[/red]")
                    return 1

        console.print("\n[bold green]Verification done.[/bold green]")

        # Return error code if there were unverified patches
        return 1 if unverified_patches else 0


def main():
    parser = argparse.ArgumentParser(
        description="Verify security patches from Android Security Bulletins",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 2025-09-01 --android-version 15          # Verify patches for Android 15
  %(prog)s 2025-09-01 --android-version 16          # Verify patches for Android 16
  %(prog)s 2025-07-01 2025-08-01 --android-version 16  # Multiple bulletins
  %(prog)s 2025-09-01 --android-version 15 --update-security-patch-level  # Verify and update security patch level
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
        "--update-security-patch-level",
        action="store_true",
        help="Update security patch level and create commit if all patches are verified"
    )

    args = parser.parse_args()

    try:
        verifier = BulletinVerifier(
            bulletin_dates=args.bulletin_dates,
            android_version=args.android_version,
            max_workers=args.workers,
            verbose=args.verbose,
            update_security_patch_level=args.update_security_patch_level
        )

        return verifier.run()

    except KeyboardInterrupt:
        console.print("\n[red]Aborted by user[/red]")
        return 130
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
