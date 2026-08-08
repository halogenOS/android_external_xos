#!/usr/bin/env python3

import os
import sys
import signal
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
import git
from github import Github, GithubException
from rich.console import Console
import fcntl
import time
import re
from rapidfuzz import fuzz
from datetime import datetime, timedelta

# Configuration constants (configurable through environment variables)
GITLAB_URL = os.environ.get('XOS_GITLAB_URL', 'https://git.halogenos.org')
GITLAB_GROUP_ID = int(os.environ.get('XOS_GITLAB_GROUP_ID', '108'))  # halogenOS group ID
GITHUB_ORG = os.environ.get('XOS_GITHUB_ORG', 'halogenOS')

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

@dataclass
class ProjectMapping:
    """Mapping between AOSP repository and local project."""
    local_path: str
    local_name: str
    aosp_name: str
    remote: str
    revision: str

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
    def get_truncated_commits(repo_path: Path) -> List[str]:
        """Commits where history genuinely stops.

        git reports a repository as shallow for as long as its `shallow` file is
        non-empty, and nothing ever prunes that file: once later fetches have
        filled the history in, the entries linger and every shallow check keeps
        answering yes. Only an entry whose parent objects are actually absent
        truncates anything, so read the raw commit objects (which bypasses the
        grafting that hides those parents) and keep the entries that still bite.
        """
        try:
            repo = git.Repo(repo_path)
            shallow_file = Path(repo_path) / repo.git.rev_parse('--git-path', 'shallow')
            if not shallow_file.exists():
                return []

            truncated = []
            for sha in shallow_file.read_text().split():
                raw = repo.git.cat_file('commit', sha)
                parents = [line.split()[1] for line in raw.splitlines()
                           if line.startswith('parent ')]
                for parent in parents:
                    try:
                        repo.git.cat_file('-e', parent)
                    except git.exc.GitCommandError:
                        truncated.append(sha)
                        break

            return truncated
        except Exception as e:
            print(f"Error determining truncated history: {e}")
            return []

    @staticmethod
    def refs_containing(repo_path: Path, commit: str, ref_prefix: str) -> List[str]:
        """Refs below ref_prefix that have commit in their history."""
        try:
            repo = git.Repo(repo_path)
            output = repo.git.for_each_ref('--format=%(refname)', '--contains', commit, ref_prefix)
            return [line for line in output.splitlines() if line]
        except Exception as e:
            print(f"Error finding refs containing {commit}: {e}")
            return []

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

    # Namespace for mirroring bookkeeping refs, so that a remote's tags can be
    # tracked per remote instead of being merged into the local refs/tags/*.
    MIRROR_STATE_NAMESPACE = "refs/mirror-state"

    @staticmethod
    def fetch_remote(repo_path: Path, remote_name: str, prune: bool = False,
                     refspec: Optional[str] = None) -> bool:
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(remote_name)
            kwargs = {}
            if prune:
                kwargs['prune'] = True
            if refspec:
                remote.fetch(refspec=refspec, force=True, **kwargs)
            else:
                remote.fetch(**kwargs)
            return True
        except Exception as e:
            print(f"Error fetching from {remote_name}: {e}")
            return False

    @staticmethod
    def fetch_remote_tags(repo_path: Path, remote_name: str) -> bool:
        """Fetch a remote's tags into a per-remote namespace."""
        namespace = GitOperations._tag_namespace(remote_name)
        return GitOperations.fetch_remote(repo_path, remote_name, prune=True,
                                          refspec=f"+refs/tags/*:{namespace}/*")

    @staticmethod
    def _tag_namespace(remote_name: str) -> str:
        return f"{GitOperations.MIRROR_STATE_NAMESPACE}/{remote_name}/tags"

    @staticmethod
    def remote_tag_ref(remote_name: str, tag_name: str) -> str:
        """Ref holding a remote's copy of a tag, requires fetch_remote_tags()."""
        return f"{GitOperations._tag_namespace(remote_name)}/{tag_name}"

    @staticmethod
    def _ref_shas(repo_path: Path, prefix: str) -> Dict[str, str]:
        """Map ref names below prefix to the object they point at."""
        try:
            repo = git.Repo(repo_path)
            output = repo.git.for_each_ref('--format=%(refname)%09%(objectname)', prefix)
        except Exception as e:
            print(f"Error listing refs below {prefix}: {e}")
            return {}

        shas = {}
        for line in output.splitlines():
            refname, _, sha = line.partition('\t')
            name = refname[len(prefix) + 1:]
            if name and name != 'HEAD':
                shas[name] = sha
        return shas

    @staticmethod
    def is_ancestor(repo_path: Path, commit: str, descendant: str) -> bool:
        """Whether commit is reachable from descendant, i.e. a push would fast-forward."""
        try:
            repo = git.Repo(repo_path)
            repo.git.merge_base('--is-ancestor', commit, descendant)
            return True
        except git.exc.GitCommandError:
            return False
        except Exception as e:
            print(f"Error comparing {commit} and {descendant}: {e}")
            return False

    @staticmethod
    def get_remote_branch_shas(repo_path: Path, remote_name: str) -> Dict[str, str]:
        """Map remote branch names to their commit, from remote-tracking refs."""
        return GitOperations._ref_shas(repo_path, f"refs/remotes/{remote_name}")

    @staticmethod
    def get_remote_tag_shas(repo_path: Path, remote_name: str) -> Dict[str, str]:
        """Map a remote's tag names to their object, requires fetch_remote_tags()."""
        return GitOperations._ref_shas(repo_path, GitOperations._tag_namespace(remote_name))

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

    # A server that refuses an oversized pack reports it while unpacking, so the
    # rejection names the receiving end rather than a size.
    PACK_TOO_BIG_MARKERS = (
        'unpack failed',
        'index-pack failed',
        'pack exceeds maximum allowed size',
        'remote end hung up',
        'early eof',
        'the remote end hung up unexpectedly',
        'http code = 413',
    )

    @staticmethod
    def run_push(repo_path: Path, remote_name: str, refspec: str) -> tuple[bool, str]:
        """Push one refspec, returning everything the remote said about it.

        The interesting part of a rejection is the remote's own message, which
        arrives on stderr and never reaches the per-ref status, so capture both
        streams instead of inspecting the parsed result.
        """
        repo = git.Repo(repo_path)
        status, stdout, stderr = repo.git.execute(
            ['git', 'push', '--porcelain', remote_name, refspec],
            with_extended_output=True, with_exceptions=False)

        message = '\n'.join(part.strip() for part in (stdout, stderr) if part and part.strip())
        rejected = any(line.startswith('!') for line in (stdout or '').splitlines())
        return status == 0 and not rejected, message

    @staticmethod
    def is_pack_too_big(message: str) -> bool:
        """Whether a push failure looks like the pack being too large to accept."""
        lowered = (message or '').lower()
        return any(marker in lowered for marker in GitOperations.PACK_TOO_BIG_MARKERS)

    @staticmethod
    def push_branch_in_chunks(repo_path: Path, source_remote: str, source_branch: str,
                              dest_remote: str, dest_branch: str,
                              on_progress=None) -> tuple[bool, str]:
        """Advance a branch on the remote in chunks of history.

        A single push has to be accepted as one pack, so a branch carrying more
        history than the server will take can never go up in one attempt. Walk
        it instead: push an intermediate commit, and the branch fast-forwards to
        it, which shrinks what the next attempt has to carry. The chunk halves
        on every rejection and doubles again after every success, so the walk
        settles on a size the server accepts without rediscovering the limit
        from scratch each time.
        """
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(dest_remote)
            target = f'{source_remote}/{source_branch}'

            # Only the commits the mirror is missing have to be walked.
            try:
                base = repo.git.rev_parse(f'{dest_remote}/{dest_branch}')
                commit_range = f'{base}..{target}'
            except git.exc.GitCommandError:
                commit_range = target

            # Topological order guarantees a commit's ancestors come before it,
            # so pushing the commit at any position sends only what precedes it.
            commits = repo.git.rev_list('--topo-order', '--reverse', commit_range).split()
            if not commits:
                return True, "Already up-to-date"

            total = len(commits)
            pushed = 0
            chunk = total
            rejected_size = None  # smallest chunk the remote has refused
            pushes = 0
            rejections = 0

            while pushed < total:
                attempt = min(chunk, total - pushed)
                boundary = commits[pushed + attempt - 1]
                pushes += 1

                if on_progress:
                    on_progress(pushed, total, attempt)

                ok, failure = GitOperations.run_push(
                    repo_path, dest_remote, f'{boundary}:refs/heads/{dest_branch}')

                if ok:
                    pushed += attempt
                    # Reach further only while staying under the smallest size
                    # the remote has already refused: a rejection costs a full
                    # upload, so a size known to fail must never be retried.
                    if rejected_size is None or chunk * 2 < rejected_size:
                        chunk *= 2
                    continue

                if not GitOperations.is_pack_too_big(failure):
                    return False, failure

                if attempt <= 1:
                    return False, f"single commit exceeds the remote's pack limit: {failure}"

                rejections += 1
                rejected_size = attempt
                chunk = max(attempt // 2, 1)

            return True, (f"{total} commits in {pushes - rejections} chunks "
                          f"({rejections} rejected while finding the size)")

        except Exception as e:
            return False, str(e)

    @staticmethod
    def push_branch(repo_path: Path, source_remote: str, source_branch: str,
                   dest_remote: str, dest_branch: str) -> tuple[bool, str]:
        try:
            repo = git.Repo(repo_path)
            refspec = f'{source_remote}/{source_branch}:refs/heads/{dest_branch}'
            ok, message = GitOperations.run_push(repo_path, dest_remote, refspec)

            if ok:
                return True, "Already up-to-date" if 'up to date' in message.lower() else "Success"

            lowered = message.lower()
            if 'non-fast-forward' in lowered or 'fetch first' in lowered:
                # Report the name the diverged branch can be preserved under.
                try:
                    commit = repo.remote(source_remote).refs[source_branch].commit
                    return False, f"{dest_branch}-{commit.hexsha[:7]}"
                except Exception:
                    return False, "non-fast-forward"

            return False, message

        except Exception as e:
            return False, f"Error: {str(e)}"

    @staticmethod
    def push_tag_from_ref(repo_path: Path, source_ref: str, tag_name: str,
                          remote_name: str) -> bool:
        """Push an arbitrary ref to the remote as a tag.

        Lets a mirror relay another remote's tags without creating them locally.
        """
        try:
            repo = git.Repo(repo_path)
            remote = repo.remote(remote_name)
            push_infos = remote.push(refspec=f'{source_ref}:refs/tags/{tag_name}')

            for info in push_infos:
                if info.flags & info.ERROR:
                    return False

            return True
        except Exception as e:
            print(f"Error pushing tag {tag_name}: {e}")
            return False

    @staticmethod
    def push_tag(repo_path: Path, tag_name: str, remote_name: str) -> bool:
        """Push a local tag to remote repository."""
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
    # First try environment variables
    top = os.environ.get('TOP') or os.environ.get('ANDROID_BUILD_TOP')
    if top:
        return Path(top)

    # Walk up the directory tree looking for .repo
    current_dir = Path.cwd()
    while current_dir != Path('/'):
        repo_dir = current_dir / '.repo'
        if repo_dir.exists() and repo_dir.is_dir():
            return current_dir
        current_dir = current_dir.parent

    # If we reach here, we didn't find .repo
    raise RuntimeError("Cannot find Android source tree. Either set TOP/ANDROID_BUILD_TOP environment variable or run from within Android source tree.")
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

def get_gitlab_token() -> Optional[str]:
    """Read GitLab token from credentials file."""
    token_path = Path.home() / ".creds" / "xos_gitlab_token"
    try:
        with open(token_path, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except Exception as e:
        console.print(f"[yellow]Error reading GitLab token: {e}[/yellow]")
        return None

def check_if_xos_repo_exists(repo_url: str, repo_path: Path) -> bool:
    """Check if XOS repository exists using git ls-remote."""
    try:
        repo = git.Repo(repo_path)
        # Disable authentication prompts for the existence check
        with repo.git.custom_environment(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true'):
            repo.git.ls_remote(repo_url)
        return True
    except git.exc.GitCommandError:
        return False
    except Exception as e:
        console.print(f"[yellow]Error checking repository existence: {e}[/yellow]")
        return False

def create_xos_repo(repo_name: str) -> bool:
    """Create a XOS repository on the GitLab server using the API."""
    try:
        import gitlab

        gitlab_token = get_gitlab_token()
        if not gitlab_token:
            console.print("[yellow]Warning: No GitLab token found, cannot create repository[/yellow]")
            return False

        gl = gitlab.Gitlab(GITLAB_URL, private_token=gitlab_token)
        gl.auth()

        # Create the repository
        project_data = {
            'name': repo_name,
            'namespace_id': GITLAB_GROUP_ID,
            'visibility': 'public'
        }

        project = gl.projects.create(project_data)
        console.print(f"[green]Created GitLab repository: {repo_name}[/green]")
        return True

    except Exception as e:
        error_str = str(e)
        # Check if repo already exists
        if "has already been taken" in error_str or "already been taken" in error_str:
            console.print(f"[cyan]Repository {repo_name} already exists[/cyan]")
            return True
        console.print(f"[red]Failed to create GitLab repository {repo_name}: {e}[/red]")
        return False

def create_xos(repo_name: str) -> bool:
    """
    Create XOS repository on GitLab and/or GitHub depending on available tokens.

    Returns True if at least one repository was created (or already exists).
    """
    created_anywhere = False

    # Try GitLab
    if get_gitlab_token():
        if create_xos_repo(repo_name):
            created_anywhere = True
    # else: GitLab token not available, skipping (expected in some environments)

    # Try GitHub
    github_token = get_github_token()
    if github_token:
        if create_github_repo(repo_name, github_token):
            created_anywhere = True
    # else: GitHub token not available, skipping (expected in some environments)

    return created_anywhere

# Global console for shared use
console = Console()

def handle_lfs_cleanup(repo_path: Path, dry_run: bool = False) -> Tuple[bool, str]:
    """Handle Git LFS cleanup (equivalent to unLFS function)."""
    try:
        repo = git.Repo(repo_path)

        # Check if LFS is present
        lfsconfig_exists = (repo_path / ".lfsconfig").exists()
        gitattributes_has_lfs = False

        gitattributes_path = repo_path / ".gitattributes"
        if gitattributes_path.exists():
            with open(gitattributes_path, 'r') as f:
                gitattributes_has_lfs = 'merge=lfs' in f.read()

        if not (lfsconfig_exists or gitattributes_has_lfs):
            return True, "no LFS detected"

        if dry_run:
            return True, "would handle LFS cleanup (dry run)"

        # Equivalent of the shell script unLFS function
        commands = [
            ["git", "lfs", "install"],
            ["git", "lfs", "fetch"],
            ["git", "lfs", "checkout"]
        ]

        # Run LFS commands
        for cmd in commands:
            try:
                subprocess.run(cmd, cwd=repo_path, check=False, capture_output=True, text=True)
            except Exception:
                pass  # Ignore errors, just like the shell script with || :

        # Get LFS files
        result = subprocess.run(
            ["git", "lfs", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            lfs_files = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 3:
                    lfs_files.append(parts[2])

            if lfs_files:
                # Remove from cache and untrack
                for lfs_file in lfs_files:
                    subprocess.run(["git", "rm", "--cached", lfs_file], cwd=repo_path, check=False, capture_output=True)
                    subprocess.run(["git", "lfs", "untrack", lfs_file], cwd=repo_path, check=False, capture_output=True)

        # Remove LFS config files
        for config_file in [".gitattributes", ".lfsconfig"]:
            config_path = repo_path / config_file
            if config_path.exists():
                config_path.unlink()

        # Stage and commit removal
        try:
            # Use git.index.remove() for deleted files instead of add()
            files_to_remove = []
            if not (repo_path / ".gitattributes").exists():
                files_to_remove.append(".gitattributes")
            if not (repo_path / ".lfsconfig").exists():
                files_to_remove.append(".lfsconfig")

            if files_to_remove:
                # Stage the removal of deleted files
                for file in files_to_remove:
                    try:
                        repo.index.remove([file])
                    except Exception:
                        pass  # File might not be in index

            # Add any existing files
            existing_files = []
            if (repo_path / ".gitattributes").exists():
                existing_files.append(".gitattributes")
            if (repo_path / ".lfsconfig").exists():
                existing_files.append(".lfsconfig")

            if existing_files:
                repo.index.add(existing_files)

            repo.index.commit("Un-LFS")
        except Exception:
            pass  # Ignore if nothing to commit

        # Uninstall LFS
        subprocess.run(["git", "lfs", "uninstall"], cwd=repo_path, check=False, capture_output=True)

        # Add LFS files directly (equivalent to final part of shell script)
        if lfs_files:
            try:
                repo.index.add(lfs_files)
                repo.index.commit("Directly checkout LFS files")
            except Exception:
                pass  # Ignore if nothing to commit

        return True, "LFS cleanup completed"

    except Exception as e:
        return False, f"LFS cleanup failed: {str(e)}"

def run_repo_command(command: str, cwd: Path, dry_run: bool = False) -> bool:
    """Run a repo command in the specified directory."""
    if dry_run:
        console.print(f"[blue][DRY RUN] Would run: {command}[/blue]")
        return True

    try:
        # Use Popen for real-time output streaming
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
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
            console.print(f"\n[red]Command failed with return code {process.returncode}[/red]")
            return False
        return True
    except Exception as e:
        console.print(f"[red]Failed to run {command}: {e}[/red]")
        return False

def safe_get_remote(repo: git.Repo, remote_name: str) -> Optional[git.Remote]:
    """Safely get a remote from a repo, returning None if it doesn't exist."""
    try:
        if remote_name in [remote.name for remote in repo.remotes]:
            return repo.remote(remote_name)
        return None
    except Exception:
        return None

def safe_add_or_update_remote(repo: git.Repo, remote_name: str, url: str) -> git.Remote:
    """Safely add or update a remote in a repository."""
    existing_remote = safe_get_remote(repo, remote_name)
    if existing_remote:
        if existing_remote.url != url:
            existing_remote.set_url(url)
        return existing_remote
    else:
        return repo.create_remote(remote_name, url)

def get_aosp_remote_url(aosp_project_name: str) -> str:
    """Get the AOSP remote URL for a given project name."""
    return f"https://android.googlesource.com/{aosp_project_name}"

def ensure_aosp_remote(repo: git.Repo, aosp_project_name: str) -> git.Remote:
    """Ensure AOSP remote exists in repository and return it."""
    aosp_url = get_aosp_remote_url(aosp_project_name)
    return safe_add_or_update_remote(repo, 'aosp', aosp_url)

def generate_manifest(top: Path, dry_run: bool = False) -> Path:
    """Generate temporary manifest file."""
    manifest_path = top / "full-manifest.xml"

    try:
        if dry_run:
            console.print("[blue][DRY RUN] Generating temporary manifest for analysis[/blue]")

        result = subprocess.run(
            ["repo", "manifest"],
            cwd=top,
            capture_output=True,
            text=True,
            check=True
        )

        with open(manifest_path, 'w') as f:
            f.write(result.stdout)

        return manifest_path
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to generate manifest: {e}[/red]")
        raise

def cleanup_manifest(manifest_path: Path, dry_run: bool = False):
    """Clean up temporary manifest file."""
    if not dry_run and manifest_path.exists():
        try:
            manifest_path.unlink()
            console.print("[cyan]Deleted temporary manifest file[/cyan]")
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to delete manifest file: {e}[/yellow]")

def truncate_project_name(name: str, max_len: int = 45) -> str:
    """Truncate project name with ellipsis in the middle for table display."""
    if len(name) <= max_len:
        return name

    side_len = (max_len - 1) // 2
    return f"{name[:side_len]}…{name[-side_len:]}"


def build_project_mappings(manifest_path: Path) -> Dict[str, ProjectMapping]:
    """Build mapping from AOSP repository names to local project info."""
    mappings = {}

    top = get_android_top()
    snippet_path = top / ".repo/manifests/snippets/XOS.xml"
    aosp_snippet_path = top / ".repo/manifests/default.xml"

    # Parse the generated manifest first to get all project info
    manifest_tree = ET.parse(manifest_path)
    manifest_root = manifest_tree.getroot()

    # Get default remote and revision from manifest
    default = manifest_root.find('default')
    default_remote = default.get('remote', 'XOS') if default is not None else 'XOS'
    rom_revision = os.environ.get('ROM_REVISION', 'XOS-16.2')
    default_revision = default.get('revision', f'refs/heads/{rom_revision}') if default is not None else f'refs/heads/{rom_revision}'

    # Build remotes map
    remotes = {}
    for remote in manifest_root.findall('remote'):
        remote_name = remote.get('name')
        remote_revision = remote.get('revision', default_revision)
        remotes[remote_name] = remote_revision

    # Parse XOS snippet to understand project attributes
    xos_projects = {}
    if snippet_path.exists():
        xos_tree = ET.parse(snippet_path)
        xos_root = xos_tree.getroot()
        for project in xos_root.findall('project'):
            path = project.get('path')
            if path:
                xos_projects[path] = project

    # Parse AOSP manifest to understand AOSP repository names
    aosp_projects = {}
    if aosp_snippet_path.exists():
        aosp_tree = ET.parse(aosp_snippet_path)
        aosp_root = aosp_tree.getroot()
        for project in aosp_root.findall('project'):
            path = project.get('path')
            name = project.get('name')
            if path and name:
                aosp_projects[path] = name

    # Process all projects in manifest
    for project in manifest_root.findall('project'):
        path = project.get('path')
        name = project.get('name')
        remote = project.get('remote', default_remote)
        revision = project.get('revision')

        if not path or not name:
            continue

        if not revision:
            revision = remotes.get(remote, default_revision)

        short_revision = revision.replace('refs/heads/', '')

        # Determine AOSP repository name
        aosp_name = None

        # First check if this project exists in XOS snippet
        xos_project = xos_projects.get(path)
        if xos_project is not None:
            # Check if it has merge-aosp attribute
            if xos_project.get('merge-aosp') == 'true':
                # Get AOSP name from default.xml
                aosp_name = aosp_projects.get(path)
                if not aosp_name:
                    # Fallback: construct from platform/ + path
                    aosp_name = f"platform/{path}"

        # If not found in XOS projects, check if it exists in AOSP manifest
        if not aosp_name:
            aosp_name = aosp_projects.get(path)

        # If we have an AOSP name, create mapping
        if aosp_name:
            # Normalize AOSP name (remove platform/ prefix for matching)
            normalized_aosp_name = aosp_name.replace('platform/', '') if aosp_name.startswith('platform/') else aosp_name

            mapping = ProjectMapping(
                local_path=path,
                local_name=name,
                aosp_name=aosp_name,
                remote=remote,
                revision=short_revision
            )

            # Store both with and without platform/ prefix for flexible matching
            mappings[aosp_name] = mapping
            mappings[normalized_aosp_name] = mapping

            # Also store by local path for additional matching
            mappings[path] = mapping

    return mappings


def find_matching_projects(patch_repo_name: str, project_mappings: Dict[str, ProjectMapping]) -> List[ProjectMapping]:
    """Find local projects that match a patch repository name."""
    matches = []

    # Direct exact match
    if patch_repo_name in project_mappings:
        matches.append(project_mappings[patch_repo_name])
        return matches

    # Try without platform/ prefix
    if patch_repo_name.startswith('platform/'):
        clean_name = patch_repo_name[9:]  # Remove 'platform/'
        if clean_name in project_mappings:
            matches.append(project_mappings[clean_name])
            return matches

    # Try with platform/ prefix if not present
    if not patch_repo_name.startswith('platform/'):
        platform_name = f"platform/{patch_repo_name}"
        if platform_name in project_mappings:
            matches.append(project_mappings[platform_name])
            return matches

    # Path-based matching - check if repo name ends with any project path
    for mapping in project_mappings.values():
        if mapping in matches:  # Avoid duplicates
            continue

        # Check if patch repo name ends with the local path
        if patch_repo_name.endswith(mapping.local_path):
            matches.append(mapping)
            continue

        # Check if local path ends with part of patch repo name
        if mapping.local_path in patch_repo_name:
            matches.append(mapping)
            continue

    return matches


def filter_patches_by_android_version(bulletin_data: List[Dict], android_version: str) -> List[Dict]:
    """Filter patches that apply to the specified Android version."""
    filtered_patches = []

    for patch_level_data in bulletin_data:
        for patch in patch_level_data.get('patches', []):
            android_versions = patch.get('android_versions')
            if android_versions is None:
                # No version info, include all patches (e.g., kernel patches)
                filtered_patches.append(patch)
            elif isinstance(android_versions, list) and android_version in android_versions:
                # Version matches
                filtered_patches.append(patch)

    return filtered_patches


def find_similar_commit_by_message(repo: git.Repo, target_commit_ref: str, similarity_threshold: float = 0.9, until_ref: Optional[str] = None) -> Optional[str]:
    """
    Find commits with similar messages using fuzzy matching and date-based optimization.

    Args:
        repo: Git repository object
        target_commit_ref: Reference to the target commit we're looking for
        similarity_threshold: Minimum similarity ratio (0.0-1.0) to consider a match
        until_ref: Optional ref to limit search to (e.g., upstream branch)

    Returns:
        Commit hash of similar commit if found, None otherwise
    """
    try:
        # Get the target commit object and its full message
        target_commit = repo.commit(target_commit_ref)
        target_message = target_commit.message.strip()
        target_date = target_commit.authored_datetime

        # Optimize search by limiting to commits from target date onwards
        # Search from 1 second before target date to avoid time precision issues
        search_since = target_date - timedelta(seconds=1)

        # Get commit log with date filtering
        log_args = [
            '--oneline',
            '--since', search_since.strftime('%Y-%m-%d %H:%M:%S'),
            '--format=%H %s'  # Hash and subject line
        ]

        # If until_ref is specified, search only in that ref's history
        if until_ref:
            log_args.append(until_ref)  # Search only in that ref's history

        commit_lines = repo.git.log(*log_args).strip()
        if not commit_lines:
            return None

        # Parse commits and check similarity
        for line in commit_lines.split('\n'):
            if not line.strip():
                continue

            parts = line.split(' ', 1)
            if len(parts) < 2:
                continue

            commit_hash = parts[0]
            commit_subject = parts[1]

            # Skip if it's the same commit
            if commit_hash.startswith(target_commit_ref[:7]):
                continue

            # Get full commit message for comparison
            try:
                candidate_commit = repo.commit(commit_hash)
                candidate_message = candidate_commit.message.strip()

                # Use rapidfuzz for fuzzy matching
                similarity = fuzz.ratio(target_message, candidate_message) / 100.0

                if similarity >= similarity_threshold:
                    return commit_hash

            except Exception:
                # Skip commits we can't access
                continue

        return None

    except Exception:
        return None


def is_project_tracked_in_xos(project_path: str) -> bool:
    """
    Check if a project path is tracked in XOS.xml snippet.

    Args:
        project_path: The path attribute of the project to check

    Returns:
        True if project is tracked in XOS.xml, False otherwise
    """
    try:
        top = get_android_top()
        xos_snippet_path = top / "manifest/snippets/XOS.xml"

        if not xos_snippet_path.exists():
            return False

        tree = ET.parse(xos_snippet_path)
        root = tree.getroot()

        # Check for project with matching path
        for project in root.findall('project'):
            if project.get('path') == project_path:
                return True

        # Also check remove.xml for remove-project with matching path
        remove_xml_path = top / "manifest/snippets/remove.xml"
        if remove_xml_path.exists():
            remove_tree = ET.parse(remove_xml_path)
            remove_root = remove_tree.getroot()
            for remove_project in remove_root.findall('remove-project'):
                if remove_project.get('path') == project_path:
                    return True

        return False

    except Exception:
        return False


def find_project_in_default_manifest(project_path: str) -> Optional[ET.Element]:
    """
    Find a project element in the default manifest by path.

    Args:
        project_path: The path attribute of the project to find

    Returns:
        Project element if found, None otherwise
    """
    try:
        top = get_android_top()
        default_manifest_path = top / "manifest/default.xml"

        if not default_manifest_path.exists():
            console.print(f"[red]Default manifest not found at {default_manifest_path}[/red]")
            return None

        tree = ET.parse(default_manifest_path)
        root = tree.getroot()

        # Find project with matching path
        for project in root.findall('project'):
            if project.get('path') == project_path:
                return project

        return None

    except Exception as e:
        console.print(f"[red]Error parsing default manifest: {e}[/red]")
        return None


def create_branch_from_upstream(repo_path: Path, upstream_remote: str, upstream_branch: str,
                               target_branch: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Create a new branch from an upstream branch.

    Args:
        repo_path: Path to the repository
        upstream_remote: Name of the upstream remote
        upstream_branch: Branch name on the upstream remote
        target_branch: Name of the branch to create
        dry_run: If True, only check what would be done

    Returns:
        Tuple of (success, message)
    """
    try:
        repo = git.Repo(repo_path)

        if dry_run:
            # Check if target branch already exists locally
            if target_branch in [head.name for head in repo.heads]:
                return True, f"would checkout existing branch {target_branch}"

            # Check if upstream branch exists
            try:
                upstream_ref = f"{upstream_remote}/{upstream_branch}"
                repo.commit(upstream_ref)
                return True, f"would create {target_branch} from {upstream_ref}"
            except (git.exc.BadName, git.exc.GitCommandError):
                return False, f"upstream branch {upstream_ref} not found"

        # Check if target branch already exists locally
        if target_branch in [head.name for head in repo.heads]:
            console.print(f"[yellow]Branch {target_branch} already exists, checking it out[/yellow]")
            repo.heads[target_branch].checkout()
            return True, f"checked out existing branch {target_branch}"

        # Create new branch from upstream
        upstream_ref = f"{upstream_remote}/{upstream_branch}"
        try:
            # Verify upstream reference exists
            upstream_commit = repo.commit(upstream_ref)
            # Create and checkout new branch
            new_branch = repo.create_head(target_branch, upstream_commit)
            new_branch.checkout()
            return True, f"created branch {target_branch} from {upstream_ref}"
        except (git.exc.BadName, git.exc.GitCommandError) as e:
            return False, f"failed to create branch from {upstream_ref}: {str(e)}"

    except Exception as e:
        return False, f"repository error: {str(e)}"


def merge_branch_into_current(repo_path: Path, source_branch: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Merge a source branch into the currently checked out branch.

    Args:
        repo_path: Path to the repository
        source_branch: Name of the branch to merge from
        dry_run: If True, only check what would be done

    Returns:
        Tuple of (success, message)
    """
    try:
        repo = git.Repo(repo_path)
        current_branch = repo.active_branch.name

        if dry_run:
            # Check if source branch exists
            if source_branch in [head.name for head in repo.heads]:
                return True, f"would merge {source_branch} into {current_branch}"
            else:
                return False, f"source branch {source_branch} not found"

        # Check if source branch exists
        if source_branch not in [head.name for head in repo.heads]:
            return False, f"source branch {source_branch} not found"

        # Check for dirty working tree
        if repo.is_dirty(untracked_files=True):
            return False, "repository has unstaged changes or untracked files"

        # Perform the merge
        try:
            # Get current HEAD before merge
            pre_merge_commit = repo.head.commit.hexsha

            # Perform merge
            repo.git.merge(source_branch)

            # Check if anything was actually merged
            post_merge_commit = repo.head.commit.hexsha

            if pre_merge_commit == post_merge_commit:
                return True, f"merge of {source_branch} resulted in no changes (already up-to-date)"
            else:
                return True, f"successfully merged {source_branch} into {current_branch}"

        except git.exc.GitCommandError as e:
            # Check if it's a merge conflict
            if "CONFLICT" in str(e) or "conflict" in str(e).lower():
                return True, f"merge conflicts detected when merging {source_branch} - please resolve manually"
            else:
                return False, f"merge failed: {str(e)}"

    except Exception as e:
        return False, f"repository error: {str(e)}"


def branch_exists_locally(repo_path: Path, branch_name: str) -> bool:
    """
    Check if a branch exists locally in the repository.

    Args:
        repo_path: Path to the repository
        branch_name: Name of the branch to check

    Returns:
        True if branch exists locally, False otherwise
    """
    try:
        repo = git.Repo(repo_path)
        return branch_name in [head.name for head in repo.heads]
    except git.exc.InvalidGitRepositoryError:
        return False


def get_xos_target_branch_for_tracked_project(project_path: str) -> Optional[str]:
    """
    Get the XOS target branch name for a tracked project by looking at XOS remote revision.

    Args:
        project_path: Project path in manifest

    Returns:
        XOS branch name or None if not found
    """
    try:
        top = get_android_top()

        # Check XOS.xml snippet
        xos_snippet_path = top / ".repo/manifests/snippets/XOS.xml"
        if xos_snippet_path.exists():
            xos_tree = ET.parse(xos_snippet_path)
            xos_root = xos_tree.getroot()

            # Look for project in XOS snippet
            project_elem = xos_root.find(f"project[@path='{project_path}']")
            if project_elem is not None:
                revision = project_elem.get('revision')
                if revision:
                    return revision.replace('refs/heads/', '')
                else:
                    # Get revision from XOS remote specification
                    remote_name = project_elem.get('remote', 'XOS')
                    xos_remote = xos_root.find(f"remote[@name='{remote_name}']")
                    if xos_remote is not None:
                        revision = xos_remote.get('revision')
                        if revision:
                            return revision.replace('refs/heads/', '')

        return None

    except Exception:
        return None


def setup_tracked_project_branch(repo_path: Path, project_path: str, target_branch: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    Set up a branch for a newly tracked project by determining upstream info from default.xml
    and creating the branch from upstream, just like reticulate_splines does.

    Args:
        repo_path: Path to the repository
        project_path: Project path in manifest
        target_branch: Target branch name
        dry_run: If True, only check what would be done

    Returns:
        Tuple of (success, message)
    """
    try:
        top = get_android_top()
        aosp_snippet_path = top / ".repo/manifests/default.xml"

        if not aosp_snippet_path.exists():
            return False, f"default manifest not found at {aosp_snippet_path}"

        aosp_tree = ET.parse(aosp_snippet_path)
        aosp_root = aosp_tree.getroot()

        # Find project in default manifest
        project_element = aosp_root.find(f"project[@path='{project_path}']")
        if project_element is None:
            return False, f"project {project_path} not found in default manifest"

        aosp_name = project_element.get('name')
        if not aosp_name:
            return False, f"project {project_path} has no name in default manifest"

        # Get AOSP remote URL from default.xml
        aosp_remote = aosp_root.find("remote[@name='aosp']")
        if aosp_remote is None:
            return False, "AOSP remote not found in default manifest"

        aosp_fetch_url = aosp_remote.get('fetch')
        if not aosp_fetch_url:
            return False, "AOSP remote has no fetch URL"

        upstream_url = f"{aosp_fetch_url.rstrip('/')}/{aosp_name}"

        # Get upstream revision - first try project-specific, then remote, then default
        upstream_rev = project_element.get('revision')

        if not upstream_rev:
            upstream_rev = aosp_remote.get('revision')

        if not upstream_rev:
            default_aosp = aosp_root.find("default[@remote='aosp']")
            if default_aosp is not None:
                upstream_rev = default_aosp.get('revision')

        if not upstream_rev:
            return False, f"unable to determine AOSP upstream revision for {project_path}"

        # Don't clean up revision here - we need to handle refs/tags/ vs refs/heads/ later

        if dry_run:
            return True, f"would create {target_branch} from {upstream_url}@{upstream_rev}"

        # Now actually set up the branch like reticulate_splines does
        repo = git.Repo(repo_path)

        # Set up upstream remote
        upstream_remote = safe_add_or_update_remote(repo, 'upstream', upstream_url)

        # Fetch from upstream
        console.print(f"[cyan]{project_path}:[/cyan] Fetching upstream")
        upstream_remote.fetch()

        # Check if it's a tag or branch and extract the reference name
        if upstream_rev.startswith('refs/tags/'):
            # It's a tag - extract tag name
            ref_name = upstream_rev.replace('refs/tags/', '')
            is_tag = True
        elif upstream_rev.startswith('refs/heads/'):
            # It's a branch - extract branch name
            ref_name = upstream_rev.replace('refs/heads/', '')
            is_tag = False
        else:
            # Assume it's a branch name without refs/heads/
            ref_name = upstream_rev
            is_tag = False

        # Create target branch from upstream
        console.print(f"[cyan]{project_path}:[/cyan] Creating {target_branch} from upstream")

        if is_tag:
            # For tags, fetch the tag first, then checkout using just the tag name
            repo.git.fetch('upstream', f'refs/tags/{ref_name}:refs/tags/{ref_name}')
            repo.git.checkout(ref_name, B=target_branch)
        else:
            # For branches, checkout from the remote reference
            upstream_ref = f"upstream/{ref_name}"
            repo.git.checkout(upstream_ref, B=target_branch)

        return True, f"created {target_branch} from {upstream_url}@{upstream_rev}"

    except Exception as e:
        return False, f"error setting up tracked project: {str(e)}"


def track_project_in_xos(project_path: str, dry_run: bool = False) -> bool:
    """
    Track a project by adding it as a remove-project in remove.xml.
    Projects are added to the "Replaced by tracked repository" section in alphabetical order.

    Args:
        project_path: The path of the project to track
        dry_run: If True, only print what would be done

    Returns:
        True if successful, False otherwise
    """
    if is_project_tracked_in_xos(project_path):
        return True  # Already tracked

    # Find the project in default manifest
    project_element = find_project_in_default_manifest(project_path)
    if project_element is None:
        console.print(f"[red]Project {project_path} not found in default manifest[/red]")
        return False

    try:
        top = get_android_top()
        remove_xml_path = top / "manifest/snippets/remove.xml"

        if dry_run:
            console.print(f"[blue][DRY RUN] Would track {project_path} in remove.xml[/blue]")
            return True

        console.print(f"[cyan]Tracking {project_path} in XOS manifests...[/cyan]")

        if not remove_xml_path.exists():
            console.print(f"[red]remove.xml not found at {remove_xml_path}[/red]")
            return False

        # Parse the XML with comments preserved using a different approach
        tree = ET.parse(remove_xml_path)
        root = tree.getroot()

        # Create new remove-project element with same attributes as original
        new_element = ET.Element('remove-project')
        for attr_name, attr_value in project_element.attrib.items():
            new_element.set(attr_name, attr_value)

        # Find correct insertion position in alphabetical order
        insert_index = len(root)  # Default to end
        for i, child in enumerate(root):
            if hasattr(child, 'tag') and child.tag == 'remove-project':
                existing_path = child.get('path', '')
                if project_path < existing_path:
                    insert_index = i
                    break

        # Insert the element at the correct position
        root.insert(insert_index, new_element)

        # Write back with proper formatting
        # First, let's read the original to preserve structure
        with open(remove_xml_path, 'r', encoding='utf-8') as f:
            original_lines = f.readlines()

        # Find the insertion point in the original file
        insert_line_idx = len(original_lines) - 1  # Before </manifest>

        # Build the new element string with proper formatting
        attrs = []
        for attr_name, attr_value in project_element.attrib.items():
            attrs.append(f'{attr_name}="{attr_value}"')
        new_line = f'  <remove-project {" ".join(attrs)} />\n'

        # Find where to insert by comparing paths
        for i, line in enumerate(original_lines):
            stripped = line.strip()
            if stripped.startswith('<remove-project '):
                path_match = re.search(r'path="([^"]*)"', stripped)
                if path_match:
                    existing_path = path_match.group(1)
                    if project_path < existing_path:
                        insert_line_idx = i
                        break

        # Insert the new line
        original_lines.insert(insert_line_idx, new_line)

        # Write back the file
        with open(remove_xml_path, 'w', encoding='utf-8') as f:
            f.writelines(original_lines)

        # Also add to XOS.xml
        xos_xml_path = top / "manifest/snippets/XOS.xml"
        if xos_xml_path.exists():
            # Convert path to repository name following XOS pattern: external/rust/crabbyavif -> android_external_rust_crabbyavif
            repo_name = f"android_{project_path.replace('/', '_')}"

            # Read XOS.xml to find insertion point
            with open(xos_xml_path, 'r', encoding='utf-8') as f:
                xos_lines = f.readlines()

            # Build the new project line
            xos_new_line = f'  <project path="{project_path}" name="{repo_name}" remote="XOS" merge-aosp="true" />\n'

            # Find correct alphabetical position among existing projects
            xos_insert_idx = len(xos_lines) - 1  # Before </manifest>
            for i, line in enumerate(xos_lines):
                stripped = line.strip()
                if stripped.startswith('<project '):
                    path_match = re.search(r'path="([^"]*)"', stripped)
                    if path_match:
                        existing_path = path_match.group(1)
                        if project_path < existing_path:
                            xos_insert_idx = i
                            break

            # Insert the new project line
            xos_lines.insert(xos_insert_idx, xos_new_line)

            # Write back XOS.xml
            with open(xos_xml_path, 'w', encoding='utf-8') as f:
                f.writelines(xos_lines)

        # Commit the changes
        try:
            manifest_repo = git.Repo(top / "manifest")
            files_to_add = ['snippets/remove.xml']
            if xos_xml_path.exists():
                files_to_add.append('snippets/XOS.xml')
            manifest_repo.index.add(files_to_add)
            manifest_repo.index.commit(f"Track {project_path}")
            console.print(f"[green]✓[/green] Tracked {project_path} in manifest files and committed change")
            return True

        except Exception as e:
            console.print(f"[yellow]Warning: Added {project_path} to manifest files but failed to commit: {e}[/yellow]")
            return True  # Still successful, just no commit

    except Exception as e:
        console.print(f"[red]Failed to track {project_path}: {e}[/red]")
        return False
