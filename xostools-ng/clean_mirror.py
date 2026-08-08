"""Safely mirror non-conflicting GitLab refs to GitHub."""

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import git
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from xos_common import (
    GITHUB_ORG,
    GitOperations,
    console,
    create_github_repo,
    get_android_top,
    get_project_path,
)


@dataclass(frozen=True)
class CleanMirrorPlan:
    """Refs that can move toward GitLab without discarding GitHub history."""

    project_name: str
    branches: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    all_refs: bool = False
    create_github_repository: bool = False


@dataclass
class CleanMirrorResult:
    """Result of mirroring one repository."""

    project_name: str
    used_cache: bool = False
    created_repository: bool = False
    branches_pushed: int = 0
    tags_pushed: int = 0
    up_to_date: int = 0
    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedSourceRefs:
    """Fetched GitLab refs selected by one mirror plan."""

    repo_path: Path
    branches: Tuple[str, ...]
    tags: Tuple[str, ...]
    source_branches: Dict[str, str]
    source_tags: Dict[str, str]


@dataclass(frozen=True)
class PreparedRefs:
    """Fetched source and destination refs for one repository."""

    repo_path: Path
    branches: Tuple[str, ...]
    tags: Tuple[str, ...]
    source_branches: Dict[str, str]
    destination_branches: Dict[str, str]
    source_tags: Dict[str, str]
    destination_tags: Dict[str, str]


class TruncatedMirrorHistory(RuntimeError):
    """Raised when a selected ref reaches history missing from a local checkout."""


def repository_urls(project_name: str) -> Tuple[str, str]:
    """Return the GitLab source and GitHub destination SSH URLs."""
    return (
        f"git@git.halogenos.org:halogenOS/{project_name}",
        f"git@github.com:{GITHUB_ORG}/{project_name}",
    )


def discover_local_repositories(top: Path) -> Dict[str, Path]:
    """Map mirror repository names to checked-out Android project paths."""
    project_list = top / ".repo" / "project.list"
    if not project_list.exists():
        return {}

    repositories = {}
    for relative_path in project_list.read_text().splitlines():
        relative_path = relative_path.strip()
        if not relative_path:
            continue

        repo_path = top / relative_path
        if repo_path.exists():
            repositories[get_project_path(relative_path).casefold()] = repo_path
    return repositories


def cache_repository(top: Path, project_name: str, source_url: str) -> Path:
    """Return a reusable bare clone, creating it when needed."""
    cache_root = top / ".cache" / "mirror-drift" / "repositories"
    cache_root.mkdir(parents=True, exist_ok=True)
    repo_path = cache_root / f"{project_name}.git"

    if repo_path.exists():
        try:
            git.Repo(repo_path)
        except git.exc.InvalidGitRepositoryError as error:
            raise RuntimeError(f"Cache path is not a Git repository: {repo_path}") from error
        return repo_path

    try:
        git.Repo.clone_from(source_url, repo_path, bare=True, multi_options=["--no-tags"])
    except Exception:
        if repo_path.exists():
            shutil.rmtree(repo_path)
        raise
    return repo_path


def configure_and_fetch_source(repo_path: Path, project_name: str):
    """Configure and fetch the complete GitLab source state."""
    source_url, _ = repository_urls(project_name)

    if not GitOperations.add_remote(repo_path, "xos", source_url):
        raise RuntimeError("Failed to add the GitLab remote")
    if not GitOperations.fetch_remote(repo_path, "xos", prune=True):
        raise RuntimeError("Failed to fetch GitLab branches")
    if not GitOperations.fetch_remote_tags(repo_path, "xos"):
        raise RuntimeError("Failed to fetch GitLab tags")


def configure_and_fetch_destination(repo_path: Path, project_name: str):
    """Configure and fetch the complete GitHub destination state."""
    _, destination_url = repository_urls(project_name)

    if not GitOperations.add_remote(repo_path, "xosgh", destination_url):
        raise RuntimeError("Failed to add the GitHub remote")
    if not GitOperations.fetch_remote(repo_path, "xosgh", prune=True):
        raise RuntimeError("Failed to fetch GitHub branches")
    if not GitOperations.fetch_remote_tags(repo_path, "xosgh"):
        raise RuntimeError("Failed to fetch GitHub tags")


def selected_refs_reach_truncated_history(
    repo_path: Path,
    branches: Tuple[str, ...],
    tags: Tuple[str, ...],
) -> bool:
    """Whether any selected source ref reaches a genuinely shallow boundary."""
    truncated = GitOperations.get_truncated_commits(repo_path)
    if not truncated:
        return False

    branch_names = set(branches)
    tag_names = set(tags)
    branch_prefix = "refs/remotes/xos/"
    tag_prefix = GitOperations.remote_tag_ref("xos", "")

    for commit in truncated:
        for ref in GitOperations.refs_containing(repo_path, commit, branch_prefix):
            if ref[len(branch_prefix):] in branch_names:
                return True
        for ref in GitOperations.refs_containing(repo_path, commit, tag_prefix):
            if ref[len(tag_prefix):] in tag_names:
                return True
    return False


def prepare_source_refs(repo_path: Path, plan: CleanMirrorPlan) -> PreparedSourceRefs:
    """Fetch GitLab and resolve the refs selected by a mirror plan."""
    configure_and_fetch_source(repo_path, plan.project_name)

    source_branches = GitOperations.get_remote_branch_shas(repo_path, "xos")
    source_tags = GitOperations.get_remote_tag_shas(repo_path, "xos")

    if plan.all_refs:
        branches = tuple(sorted(source_branches, key=str.casefold))
        tags = tuple(sorted(source_tags, key=str.casefold))
    else:
        branches = plan.branches
        tags = plan.tags

    missing = [f"branch {name}" for name in branches if name not in source_branches]
    missing.extend(f"tag {name}" for name in tags if name not in source_tags)
    if missing:
        raise RuntimeError(f"GitLab refs disappeared: {', '.join(missing)}")

    if selected_refs_reach_truncated_history(repo_path, branches, tags):
        raise TruncatedMirrorHistory("Selected refs reach truncated local history")

    return PreparedSourceRefs(
        repo_path=repo_path,
        branches=branches,
        tags=tags,
        source_branches=source_branches,
        source_tags=source_tags,
    )


def prepare_destination_refs(
    source: PreparedSourceRefs,
    project_name: str,
) -> PreparedRefs:
    """Fetch GitHub after the source is ready and combine both ref states."""
    configure_and_fetch_destination(source.repo_path, project_name)
    return PreparedRefs(
        repo_path=source.repo_path,
        branches=source.branches,
        tags=source.tags,
        source_branches=source.source_branches,
        destination_branches=GitOperations.get_remote_branch_shas(
            source.repo_path, "xosgh"
        ),
        source_tags=source.source_tags,
        destination_tags=GitOperations.get_remote_tag_shas(source.repo_path, "xosgh"),
    )


def push_branch(refs: PreparedRefs, branch: str, result: CleanMirrorResult):
    """Push a branch only when GitHub can fast-forward to GitLab."""
    source_sha = refs.source_branches[branch]
    destination_sha = refs.destination_branches.get(branch)

    if source_sha == destination_sha:
        result.up_to_date += 1
        return
    if destination_sha and not GitOperations.is_ancestor(
        refs.repo_path, destination_sha, source_sha
    ):
        result.conflicts.append(f"B {branch}: GitHub has unique commits")
        return

    success, message = GitOperations.push_branch(
        refs.repo_path, "xos", branch, "xosgh", branch
    )
    if not success and GitOperations.is_pack_too_big(message):
        success, message = GitOperations.push_branch_in_chunks(
            refs.repo_path, "xos", branch, "xosgh", branch
        )

    if success:
        result.branches_pushed += 1
    elif (
        message in ("non-fast-forward", f"{branch}-{source_sha[:7]}")
        or "non-fast-forward" in message.lower()
        or "fetch first" in message.lower()
    ):
        result.conflicts.append(f"B {branch}: GitHub changed during mirroring")
    else:
        result.errors.append(f"B {branch}: {message}")


def push_tag(refs: PreparedRefs, tag: str, result: CleanMirrorResult):
    """Push a tag only when the name does not already exist on GitHub."""
    if tag in refs.destination_tags:
        if refs.source_tags[tag] == refs.destination_tags[tag]:
            result.up_to_date += 1
        else:
            result.conflicts.append(f"T {tag}: GitHub tag already exists")
        return

    source_ref = GitOperations.remote_tag_ref("xos", tag)
    if GitOperations.push_tag_from_ref(refs.repo_path, source_ref, tag, "xosgh"):
        result.tags_pushed += 1
        return

    if GitOperations.fetch_remote_tags(refs.repo_path, "xosgh"):
        destination_tags = GitOperations.get_remote_tag_shas(refs.repo_path, "xosgh")
        if tag in destination_tags:
            result.conflicts.append(f"T {tag}: GitHub changed during mirroring")
            return
    result.errors.append(f"T {tag}: push failed")


def execute_plan(
    plan: CleanMirrorPlan,
    top: Path,
    local_repositories: Dict[str, Path],
    github_token: str,
) -> CleanMirrorResult:
    """Mirror one plan using a local checkout or a reusable cache clone."""
    result = CleanMirrorResult(project_name=plan.project_name)
    source_url, _ = repository_urls(plan.project_name)
    local_path = local_repositories.get(plan.project_name.casefold())
    source: Optional[PreparedSourceRefs] = None

    if local_path:
        try:
            source = prepare_source_refs(local_path, plan)
        except Exception:
            source = None

    if source is None:
        result.used_cache = True
        try:
            repo_path = cache_repository(top, plan.project_name, source_url)
            source = prepare_source_refs(repo_path, plan)
        except Exception as error:
            result.errors.append(str(error))
            return result

    if plan.create_github_repository:
        if not create_github_repo(plan.project_name, github_token):
            result.errors.append("Failed to create the GitHub repository")
            return result
        result.created_repository = True

    try:
        refs = prepare_destination_refs(source, plan.project_name)
    except Exception as error:
        result.errors.append(str(error))
        return result

    for branch in refs.branches:
        push_branch(refs, branch, result)
    for tag in refs.tags:
        push_tag(refs, tag, result)

    return result


def render_results(results: List[CleanMirrorResult]) -> int:
    """Render a compact mirror summary and return the failure count."""
    branches = sum(result.branches_pushed for result in results)
    tags = sum(result.tags_pushed for result in results)
    current = sum(result.up_to_date for result in results)
    conflicts = sum(len(result.conflicts) for result in results)
    errors = sum(len(result.errors) for result in results)
    cached = sum(result.used_cache for result in results)
    created = sum(result.created_repository for result in results)

    console.print()
    heading = Text("CLEAN MIRROR", style="bold")
    heading.append(f"   {len(results)} projects")
    heading.append(f" · {branches} branches", style="green")
    heading.append(f" · {tags} tags", style="green")
    if created:
        heading.append(f" · {created} created", style="cyan")
    if cached:
        heading.append(f" · {cached} cached", style="dim")
    if current:
        heading.append(f" · {current} already current", style="dim")
    if conflicts:
        heading.append(f" · {conflicts} skipped", style="yellow")
    if errors:
        heading.append(f" · {errors} failed", style="red")
    console.print(heading)

    for result in sorted(results, key=lambda item: item.project_name.casefold()):
        for conflict in result.conflicts:
            console.print(f"  [yellow]{result.project_name}[/yellow] {conflict}")
        for error in result.errors:
            console.print(f"  [red]{result.project_name}[/red] {error}")

    return errors


def mirror_clean_plans(
    plans: List[CleanMirrorPlan],
    github_token: str,
    workers: int,
) -> int:
    """Mirror clean plans concurrently and return the number of failures."""
    if not plans:
        console.print("\n[dim]CLEAN MIRROR   nothing to push[/dim]")
        return 0

    top = get_android_top()
    local_repositories = discover_local_repositories(top)
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Mirroring clean refs", total=len(plans))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    execute_plan,
                    plan,
                    top,
                    local_repositories,
                    github_token,
                ): plan
                for plan in plans
            }

            for future in as_completed(futures):
                plan = futures[future]
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append(
                        CleanMirrorResult(
                            project_name=plan.project_name,
                            errors=[str(error)],
                        )
                    )
                progress.update(task, advance=1)

    return render_results(results)
