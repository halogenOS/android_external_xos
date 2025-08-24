#!/usr/bin/env python3
"""
Create snapshot tags for repositories.
Version: 16.0

Creates timestamped tags for all repositories specified in the manifest snippet,
allowing for easy restoration to a known state.
"""

import os
import sys
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import git

# Add xos_common to path
sys.path.insert(0, str(Path(__file__).parent))
from xos_common import (
    ManifestParser,
    GitOperations,
    get_android_top,
    ProjectInfo
)


class SnapshotCreator:
    def __init__(self, remote_name: str = "XOS", no_reset: bool = False, dry_run: bool = False):
        self.remote_name = remote_name
        self.no_reset = no_reset
        self.dry_run = dry_run
        self.top = get_android_top()
        self.snippet_path = self.top / ".repo/manifests/snippets/XOS.xml"

        if not self.snippet_path.exists():
            raise FileNotFoundError(f"Manifest snippet not found: {self.snippet_path}")

        self.manifest = ManifestParser(self.snippet_path)

    def run_repo_command(self, command: str) -> bool:
        """Run a repo command in the Android tree."""
        if self.dry_run:
            print(f"[DRY RUN] Would run: {command}")
            return True

        try:
            # Use Popen for real-time output streaming
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.top,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Stream output in real-time
            for line in iter(process.stdout.readline, ''):
                print(line, end='')

            process.wait()

            if process.returncode != 0:
                print(f"\nCommand failed with return code {process.returncode}")
                return False
            return True
        except Exception as e:
            print(f"Failed to run {command}: {e}")
            return False

    def reset_and_sync(self) -> bool:
        """Reset and sync the repo tree."""
        print("Resetting source tree back to remote state...")
        print("Any unsaved work will be gone.")

        # Source build/envsetup.sh and run reporeset
        reset_cmd = "source build/envsetup.sh && reporeset"
        if not self.run_repo_command(reset_cmd):
            print("Failed to reset repositories")
            return False

        print("Syncing repositories...")
        sync_cmd = "source build/envsetup.sh && reposync"
        if not self.run_repo_command(sync_cmd):
            print("Failed to sync repositories")
            return False

        return True

    def generate_tag_name(self, custom_tag: Optional[str] = None,
                         suffix: Optional[str] = None) -> str:
        """Generate tag name based on revision and timestamp."""
        if custom_tag:
            return f"{custom_tag}{suffix or ''}"

        revision = self.manifest.get_remote_revision(self.remote_name)
        if not revision:
            raise ValueError(f"Could not get revision for remote {self.remote_name}")

        # Generate timestamp without unix timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return f"{revision}-{timestamp}{suffix or ''}"

    def unshallow_if_needed(self, repo_path: Path) -> bool:
        """Unshallow repository if it's shallow."""
        if GitOperations.is_shallow_repo(repo_path):
            print(f"  Shallow repository detected, unshallowing...")
            if self.dry_run:
                print(f"  [DRY RUN] Would unshallow repository")
                return True
            return GitOperations.unshallow_repo(repo_path)
        return True

    def create_and_push_tag(self, repo_path: Path, tag_name: str,
                           extra_args: List[str] = None) -> bool:
        """Create and push a tag for a repository."""
        # For now, we'll use simple tag creation without message
        # Extra args support can be added later if needed
        message = f"Snapshot tag created at {datetime.now().isoformat()}"

        if self.dry_run:
            print(f"  [DRY RUN] Would create and push tag '{tag_name}' to remote '{self.remote_name}'")
            return True

        if GitOperations.create_and_push_tag(repo_path, tag_name, "HEAD",
                                            self.remote_name, message):
            print(f"  Tag {tag_name} created and pushed successfully")
            return True
        else:
            # Try without message if annotated tag fails
            if GitOperations.create_tag(repo_path, tag_name):
                if GitOperations.push_tag(repo_path, tag_name, self.remote_name):
                    print(f"  Tag {tag_name} created and pushed successfully")
                    return True
            print(f"  Failed to create/push tag")
            return False

    def process_projects(self, tag_name: str, extra_args: List[str] = None):
        """Process all projects and create tags."""
        # Get projects filtered by remote
        projects = self.manifest.get_projects_by_remote(self.remote_name)

        if not projects:
            print(f"No projects found with remote '{self.remote_name}'")
            return

        dry_run_prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{dry_run_prefix}Creating snapshot tag: {tag_name}")
        print(f"Processing {len(projects)} projects...\n")

        success_count = 0
        failed_projects = []

        for project in projects:
            project_path = self.top / project.path

            if not project_path.exists():
                print(f"Skipping {project.path}: Directory not found")
                continue

            print(f"Processing {project.path}...")

            # Unshallow if needed
            if not self.unshallow_if_needed(project_path):
                failed_projects.append(project.path)
                continue

            # Create and push tag
            if self.create_and_push_tag(project_path, tag_name, extra_args):
                success_count += 1
            else:
                failed_projects.append(project.path)

            print()  # Empty line for readability

        # Summary
        print("=" * 60)
        print(f"Snapshot creation complete!")
        print(f"Tag name: {tag_name}")
        print(f"Successfully tagged: {success_count}/{len(projects)} projects")

        if failed_projects:
            print(f"\nFailed projects ({len(failed_projects)}):")
            for project in failed_projects:
                print(f"  - {project}")

        print("\nEverything done.")

    def run(self, custom_tag: Optional[str] = None,
            tag_suffix: Optional[str] = None,
            extra_git_args: List[str] = None):
        """Main execution flow."""

        # Prepare repositories if not skipping reset
        if not self.no_reset:
            dry_run_warning = " (DRY RUN MODE - no actual changes will be made)" if self.dry_run else ""
            print(f"Warning: This will perform a reporeset and a reposync to make{dry_run_warning}")
            print("sure everything is up to date before creating the snapshot.")
            print("If you do not want that to happen, use the parameter --no-reset")
            print()

            if not self.dry_run:
                response = input("Press ENTER to continue or CTRL+C to abort: ")
                print()

            if not self.reset_and_sync():
                if not self.dry_run:
                    print("Failed to prepare repositories")
                    return 1

        # Generate tag name
        try:
            tag_name = self.generate_tag_name(custom_tag, tag_suffix)
        except ValueError as e:
            print(f"Error: {e}")
            return 1

        # Process all projects
        self.process_projects(tag_name, extra_git_args)

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Create snapshot tags for repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Create snapshot with auto-generated tag
  %(prog)s --no-reset         # Skip repository reset and sync
  %(prog)s --dry-run          # Show what would be done without making changes
  %(prog)s my-snapshot        # Use custom tag name
  %(prog)s --suffix -test     # Add suffix to auto-generated tag
        """
    )

    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Skip repository reset and sync"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making any changes"
    )

    parser.add_argument(
        "--remote",
        default="XOS",
        help="Remote name to use (default: XOS)"
    )

    parser.add_argument(
        "--suffix",
        help="Suffix to append to tag name (env: tag_to_push_suffix)"
    )

    parser.add_argument(
        "tag",
        nargs="?",
        help="Custom tag name (optional)"
    )

    parser.add_argument(
        "--git-args",
        nargs=argparse.REMAINDER,
        help="Additional git tag arguments"
    )

    args = parser.parse_args()

    # Get suffix from environment if not provided
    if not args.suffix:
        args.suffix = os.environ.get("tag_to_push_suffix", "")

    try:
        creator = SnapshotCreator(
            remote_name=args.remote,
            no_reset=args.no_reset,
            dry_run=args.dry_run
        )

        return creator.run(
            custom_tag=args.tag,
            tag_suffix=args.suffix,
            extra_git_args=args.git_args
        )

    except KeyboardInterrupt:
        print("\nAborted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
