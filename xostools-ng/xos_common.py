#!/usr/bin/env python3

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass
import git
from github import Github, GithubException

@dataclass
class GitRemote:
    name: str
    url: str

@dataclass
class ProjectInfo:
    path: str
    name: str
    remote: Optional[str] = None
    revision: Optional[str] = None

class ManifestParser:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.tree = ET.parse(manifest_path)
        self.root = self.tree.getroot()

    def get_remote_revision(self, remote_name: str = "XOS") -> Optional[str]:
        for remote in self.root.findall('.//remote'):
            if remote.get('name') == remote_name:
                revision = remote.get('revision', '')
                if revision.startswith('refs/heads/'):
                    return revision[11:]
                return revision
        return None

    def get_projects(self) -> List[ProjectInfo]:
        projects = []
        for project in self.root.findall('.//project'):
            path = project.get('path')
            name = project.get('name')
            if path and name:
                projects.append(ProjectInfo(
                    path=path,
                    name=name,
                    remote=project.get('remote'),
                    revision=project.get('revision')
                ))
        return projects

    def get_projects_by_remote(self, remote_name: str) -> List[ProjectInfo]:
        """Get all projects that use a specific remote."""
        return [p for p in self.get_projects() if p.remote == remote_name]

    def get_remote_config(self, remote_name: str) -> Optional[Dict[str, str]]:
        for remote in self.root.findall('.//remote'):
            if remote.get('name') == remote_name:
                return {
                    'name': remote.get('name'),
                    'fetch': remote.get('fetch'),
                    'revision': remote.get('revision'),
                    'review': remote.get('review')
                }
        return None

class GitOperations:
    @staticmethod
    def is_shallow_repo(repo_path: Path) -> bool:
        """Check if a repository is shallow."""
        try:
            # Check for .git/shallow file which indicates a shallow repo
            shallow_file = repo_path / ".git/shallow"
            if shallow_file.exists():
                return True

            # Alternative check using git command
            repo = git.Repo(repo_path)
            try:
                result = repo.git.rev_parse("--is-shallow-repository")
                return result.lower() == "true"
            except git.exc.GitCommandError:
                # Older git versions don't have this command
                return False
        except:
            return False

    @staticmethod
    def unshallow_repo(repo_path: Path) -> bool:
        """Unshallow a repository if it's shallow."""
        try:
            if GitOperations.is_shallow_repo(repo_path):
                repo = git.Repo(repo_path)
                repo.git.fetch("--unshallow")
                return True
            return True  # Not shallow, nothing to do
        except Exception as e:
            print(f"Error unshallowing repository: {e}")
            return False

    @staticmethod
    def add_remote(repo_path: Path, remote_name: str, remote_url: str) -> bool:
        try:
            repo = git.Repo(repo_path)

            try:
                repo.delete_remote(remote_name)
            except:
                pass

            repo.create_remote(remote_name, remote_url)
            return True
        except Exception as e:
            print(f"Error adding remote {remote_name}: {e}")
            return False

    @staticmethod
    def fetch_remote(repo_path: Path, remote_name: str) -> bool:
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(remote_name)
            remote.fetch()
            return True
        except Exception as e:
            print(f"Error fetching from {remote_name}: {e}")
            return False

    @staticmethod
    def get_remote_branches(repo_path: Path, remote_name: str) -> List[str]:
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(remote_name)

            branches = []
            for ref in remote.refs:
                if ref.remote_head != 'HEAD':
                    branches.append(ref.remote_head)

            return branches
        except Exception as e:
            print(f"Error getting branches from {remote_name}: {e}")
            return []

    @staticmethod
    def get_tags_matching(repo_path: Path, pattern: str) -> List[str]:
        try:
            repo = git.Repo(repo_path)
            import re
            regex = re.compile(pattern)

            matching_tags = []
            for tag in repo.tags:
                if regex.match(tag.name):
                    matching_tags.append(tag.name)

            return matching_tags
        except Exception as e:
            print(f"Error getting tags: {e}")
            return []

    @staticmethod
    def push_branch(repo_path: Path, source_remote: str, source_branch: str,
                   dest_remote: str, dest_branch: str) -> tuple[bool, str]:
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(dest_remote)
            refspec = f'{source_remote}/{source_branch}:refs/heads/{dest_branch}'

            try:
                # Try to push
                push_infos = remote.push(refspec=refspec, progress=None)

                # Check results
                if push_infos:
                    for info in push_infos:
                        # Check for errors
                        if info.flags & info.ERROR:
                            if 'non-fast-forward' in str(info.summary).lower():
                                # Get short SHA for tag
                                try:
                                    commit = repo.remote(source_remote).refs[source_branch].commit
                                    short_sha = commit.hexsha[:7]
                                    tag_name = f"{dest_branch}-{short_sha}"
                                    return False, tag_name
                                except:
                                    return False, "non-fast-forward"
                            return False, str(info.summary)
                        # Check for up-to-date
                        elif info.flags & info.UP_TO_DATE:
                            return True, "Already up-to-date"
                        # Otherwise it's likely successful
                        elif info.flags & info.NEW_HEAD or info.flags & info.FAST_FORWARD:
                            return True, "Success"

                # If no specific flags, assume success
                return True, "Success"

            except git.exc.GitCommandError as e:
                error_msg = str(e).lower()
                if 'non-fast-forward' in error_msg:
                    try:
                        commit = repo.remote(source_remote).refs[source_branch].commit
                        short_sha = commit.hexsha[:7]
                        tag_name = f"{dest_branch}-{short_sha}"
                        return False, tag_name
                    except:
                        return False, "non-fast-forward"
                elif 'up-to-date' in error_msg or 'up to date' in error_msg:
                    return True, "Already up-to-date"
                else:
                    return False, str(e)

        except Exception as e:
            return False, f"Error: {str(e)}"

    @staticmethod
    def push_tag(repo_path: Path, tag_name: str, remote_name: str) -> bool:
        """Push a tag to remote repository."""
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(remote_name)

            # Check if tag already exists on remote
            try:
                # Fetch tags to ensure we have latest info
                remote.fetch(tags=True)
                if f"refs/tags/{tag_name}" in [ref.path for ref in remote.refs]:
                    return True  # Already exists
            except:
                pass

            # Push the specific tag
            push_infos = remote.push(refspec=f'refs/tags/{tag_name}:refs/tags/{tag_name}')

            # Check results
            for info in push_infos:
                if info.flags & info.ERROR:
                    return False

            return True
        except Exception as e:
            print(f"Error pushing tag {tag_name}: {e}")
            return False

    @staticmethod
    def create_tag(repo_path: Path, tag_name: str, message: Optional[str] = None,
                  ref: str = "HEAD", force: bool = False) -> bool:
        """Create a tag in the repository."""
        try:
            repo = git.Repo(repo_path)

            # Check if tag exists
            if not force and tag_name in [tag.name for tag in repo.tags]:
                return True  # Already exists

            # Create tag
            if message:
                repo.create_tag(tag_name, ref=ref, message=message, force=force)
            else:
                repo.create_tag(tag_name, ref=ref, force=force)

            return True
        except Exception as e:
            print(f"Error creating tag {tag_name}: {e}")
            return False

    @staticmethod
    def create_and_push_tag(repo_path: Path, tag_name: str, commit_ref: str,
                           remote_name: str, message: Optional[str] = None) -> bool:
        """Create and push a tag to remote repository."""
        try:
            # Create the tag
            if not GitOperations.create_tag(repo_path, tag_name, message, commit_ref):
                return False

            # Push the tag
            return GitOperations.push_tag(repo_path, tag_name, remote_name)

        except Exception as e:
            print(f"Error creating/pushing tag {tag_name}: {e}")
            return False

    @staticmethod
    def reset_repo(repo_path: Path) -> bool:
        """Reset repository, aborting any pending operations."""
        try:
            repo = git.Repo(repo_path)

            # Abort any pending operations
            operations = ['rebase', 'merge', 'cherry-pick', 'revert']
            for op in operations:
                try:
                    repo.git.execute(['git', op, '--abort'])
                except:
                    pass

            # Reset to HEAD
            repo.head.reset(index=True, working_tree=True)

            # Clean untracked files
            repo.git.clean('-fdx')

            return True
        except Exception as e:
            print(f"Error resetting repository: {e}")
            return False

def get_android_top() -> Path:
    """Get the Android build tree top directory."""
    top = os.environ.get('TOP')
    if not top:
        raise RuntimeError("TOP environment variable not set. Source build/envsetup.sh first.")
    return Path(top)

def get_project_path(path: str) -> str:
    """Convert a project path to a GitHub-compatible name."""
    return f"android_{path.replace('/', '_')}"

def get_github_token() -> Optional[str]:
    """Read GitHub token from credentials file."""
    token_path = Path.home() / ".creds" / "xos_github_token"
    try:
        with open(token_path, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Error reading token: {e}")
        return None

def create_github_repo(repo_name: str, github_token: str) -> bool:
    """Create a GitHub repository in the organization."""
    try:
        g = Github(github_token)
        org = g.get_organization("halogenOS")

        try:
            org.get_repo(repo_name)
            return True  # Already exists
        except GithubException:
            pass  # Doesn't exist, create it

        org.create_repo(
            name=repo_name,
            private=False,
            has_issues=True,
            has_projects=False,
            has_wiki=False,
            auto_init=False
        )
        print(f"Created GitHub repository: {repo_name}")
        return True
    except Exception as e:
        print(f"Failed to create GitHub repository {repo_name}: {e}")
        return False
