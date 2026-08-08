"""Safely mirror non-conflicting GitLab refs to GitHub."""

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import git
from rich.console import Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from mirror_progress import CleanMirrorStats, CleanMirrorTracker, render_clean_status
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


def concise_git_message(message: str, limit: int = 300) -> str:
    """Collapse Git output to one bounded line for the final report."""
    compact = " ".join((message or "unknown Git error").split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def repository_urls(project_name: str) -> Tuple[str, str]:
    """Return the GitLab source and GitHub destination SSH URLs."""
    return (
        f"git@git.halogenos.org:halogenOS/{project_name}",
        f"git@github.com:{GITHUB_ORG}/{project_name}",
    )


def retry_operation(
    operation: Callable[[], Tuple[bool, str]],
    phase: str,
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
    attempts: int = 3,
):
    """Retry a transient Git operation while keeping the live status current."""
    last_error = "unknown Git error"
    for attempt in range(1, attempts + 1):
        note = None if attempt == 1 else f"retry {attempt}/{attempts}"
        if on_status:
            on_status(phase, note)
        success, last_error = operation()
        if success:
            return
        if attempt < attempts:
            time.sleep(attempt)
    raise RuntimeError(
        f"{phase} failed after {attempts} attempts: {concise_git_message(last_error)}"
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


def cache_repository(
    top: Path,
    project_name: str,
    source_url: str,
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
) -> Path:
    """Return a reusable bare clone, creating it when needed."""
    cache_root = top / ".cache" / "mirror-drift" / "repositories"
    cache_root.mkdir(parents=True, exist_ok=True)
    repo_path = cache_root / f"{project_name}.git"

    if repo_path.exists():
        try:
            git.Repo(repo_path)
        except git.exc.InvalidGitRepositoryError as error:
            raise RuntimeError(f"Cache path is not a Git repository: {repo_path}") from error
        if on_status:
            on_status("using cache", None)
        return repo_path

    last_error = "unknown Git error"
    for attempt in range(1, 4):
        note = None if attempt == 1 else f"retry {attempt}/3"
        if on_status:
            on_status("cloning GitLab", note)
        try:
            git.Repo.clone_from(source_url, repo_path, bare=True, multi_options=["--no-tags"])
            return repo_path
        except Exception as error:
            last_error = str(error)
            if repo_path.exists():
                shutil.rmtree(repo_path)
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(
        f"Cloning GitLab failed after 3 attempts: {concise_git_message(last_error)}"
    )


def configure_and_fetch_source(
    repo_path: Path,
    project_name: str,
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
):
    """Configure and fetch the complete GitLab source state."""
    source_url, _ = repository_urls(project_name)

    if not GitOperations.add_remote(repo_path, "xos", source_url, quiet=True):
        raise RuntimeError("Failed to add the GitLab remote")
    retry_operation(
        lambda: GitOperations.fetch_remote_with_result(repo_path, "xos", prune=True),
        "fetching GitLab branches",
        on_status,
    )
    retry_operation(
        lambda: GitOperations.fetch_remote_tags_with_result(repo_path, "xos"),
        "fetching GitLab tags",
        on_status,
    )


def configure_and_fetch_destination(
    repo_path: Path,
    project_name: str,
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
):
    """Configure and fetch the complete GitHub destination state."""
    _, destination_url = repository_urls(project_name)

    if not GitOperations.add_remote(repo_path, "xosgh", destination_url, quiet=True):
        raise RuntimeError("Failed to add the GitHub remote")
    retry_operation(
        lambda: GitOperations.fetch_remote_with_result(repo_path, "xosgh", prune=True),
        "fetching GitHub branches",
        on_status,
    )
    retry_operation(
        lambda: GitOperations.fetch_remote_tags_with_result(repo_path, "xosgh"),
        "fetching GitHub tags",
        on_status,
    )


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


def prepare_source_refs(
    repo_path: Path,
    plan: CleanMirrorPlan,
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
) -> PreparedSourceRefs:
    """Fetch GitLab and resolve the refs selected by a mirror plan."""
    configure_and_fetch_source(repo_path, plan.project_name, on_status)

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

    if on_status:
        on_status("checking source history", None)
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
    on_status: Optional[Callable[[str, Optional[str]], None]] = None,
) -> PreparedRefs:
    """Fetch GitHub after the source is ready and combine both ref states."""
    configure_and_fetch_destination(source.repo_path, project_name, on_status)
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


def push_branch(
    refs: PreparedRefs,
    branch: str,
    result: CleanMirrorResult,
    on_note: Optional[Callable[[Optional[str]], None]] = None,
) -> str:
    """Push a branch only when GitHub can fast-forward to GitLab."""
    source_sha = refs.source_branches[branch]
    destination_sha = refs.destination_branches.get(branch)

    if source_sha == destination_sha:
        result.up_to_date += 1
        return "current"
    if destination_sha and not GitOperations.is_ancestor(
        refs.repo_path, destination_sha, source_sha
    ):
        result.conflicts.append(f"B {branch}: GitHub has unique commits")
        return "skipped"

    if on_note:
        on_note("sending")
    success, message = GitOperations.push_branch(
        refs.repo_path, "xos", branch, "xosgh", branch
    )
    if not success and GitOperations.is_pack_too_big(message):
        def report(pushed, total, chunk):
            if on_note:
                on_note(f"chunked {pushed}/{total} commits, next {chunk}")

        success, message = GitOperations.push_branch_in_chunks(
            refs.repo_path,
            "xos",
            branch,
            "xosgh",
            branch,
            on_progress=report,
        )
    if on_note:
        on_note(None)

    if success:
        result.branches_pushed += 1
        return "pushed"
    if (
        message in ("non-fast-forward", f"{branch}-{source_sha[:7]}")
        or "non-fast-forward" in message.lower()
        or "fetch first" in message.lower()
    ):
        result.conflicts.append(f"B {branch}: GitHub changed during mirroring")
        return "skipped"

    result.errors.append(f"B {branch}: {concise_git_message(message)}")
    return "failed"


def push_tag(
    refs: PreparedRefs,
    tag: str,
    result: CleanMirrorResult,
    on_note: Optional[Callable[[Optional[str]], None]] = None,
) -> str:
    """Push a missing tag, staging oversized history through a temporary branch."""
    if tag in refs.destination_tags:
        if refs.source_tags[tag] == refs.destination_tags[tag]:
            result.up_to_date += 1
            return "current"
        result.conflicts.append(f"T {tag}: GitHub tag already exists")
        return "skipped"

    source_ref = GitOperations.remote_tag_ref("xos", tag)
    destination_ref = f"refs/tags/{tag}"
    if on_note:
        on_note("sending")
    success, message = GitOperations.run_push(
        refs.repo_path,
        "xosgh",
        f"{source_ref}:{destination_ref}",
    )
    if success:
        if on_note:
            on_note(None)
        result.tags_pushed += 1
        return "pushed"

    if GitOperations.fetch_remote_tags(refs.repo_path, "xosgh", quiet=True):
        destination_tags = GitOperations.get_remote_tag_shas(refs.repo_path, "xosgh")
        if tag in destination_tags:
            if on_note:
                on_note(None)
            result.conflicts.append(f"T {tag}: GitHub changed during mirroring")
            return "skipped"

    if not GitOperations.is_pack_too_big(message):
        if on_note:
            on_note(None)
        result.errors.append(f"T {tag}: {concise_git_message(message)}")
        return "failed"

    repo = git.Repo(refs.repo_path)
    try:
        target_commit = repo.git.rev_parse(f"{source_ref}^{{commit}}")
    except git.exc.GitCommandError as error:
        if on_note:
            on_note(None)
        result.errors.append(f"T {tag}: cannot resolve commit: {concise_git_message(str(error))}")
        return "failed"

    staging_branch = f"mirror-staging/{target_commit[:12]}"

    def report(pushed, total, chunk):
        if on_note:
            on_note(f"chunked {pushed}/{total} commits, next {chunk}")

    if on_note:
        on_note("too big, walking history")
    staged, stage_message = GitOperations.push_commit_in_chunks(
        refs.repo_path,
        source_ref,
        "xosgh",
        staging_branch,
        on_progress=report,
    )

    if staged:
        if on_note:
            on_note("publishing tag")
        success, message = GitOperations.run_push(
            refs.repo_path,
            "xosgh",
            f"{source_ref}:{destination_ref}",
        )
    else:
        success = False
        message = stage_message

    if on_note:
        on_note("removing staging branch")
    cleanup_ok, cleanup_message = GitOperations.run_push(
        refs.repo_path,
        "xosgh",
        f":refs/heads/{staging_branch}",
    )
    if not cleanup_ok and "remote ref does not exist" not in cleanup_message.lower():
        result.errors.append(
            f"T {tag}: failed to remove {staging_branch}: "
            f"{concise_git_message(cleanup_message)}"
        )
        if on_note:
            on_note(None)
        return "failed"

    if on_note:
        on_note(None)
    if success:
        result.tags_pushed += 1
        return "pushed"

    if GitOperations.fetch_remote_tags(refs.repo_path, "xosgh", quiet=True):
        destination_tags = GitOperations.get_remote_tag_shas(refs.repo_path, "xosgh")
        if tag in destination_tags:
            result.conflicts.append(f"T {tag}: GitHub changed during mirroring")
            return "skipped"

    result.errors.append(f"T {tag}: {concise_git_message(message)}")
    return "failed"


def execute_plan(
    plan: CleanMirrorPlan,
    top: Path,
    local_repositories: Dict[str, Path],
    github_token: str,
    tracker: Optional[CleanMirrorTracker] = None,
) -> CleanMirrorResult:
    """Mirror one plan using a local checkout or a reusable cache clone."""
    result = CleanMirrorResult(project_name=plan.project_name)
    source_url, _ = repository_urls(plan.project_name)
    local_path = local_repositories.get(plan.project_name.casefold())
    source: Optional[PreparedSourceRefs] = None
    on_status = tracker.status if tracker else None

    if local_path:
        if tracker:
            tracker.status("using local repository")
        try:
            source = prepare_source_refs(local_path, plan, on_status)
        except Exception:
            source = None

    if source is None:
        result.used_cache = True
        try:
            repo_path = cache_repository(top, plan.project_name, source_url, on_status)
            source = prepare_source_refs(repo_path, plan, on_status)
        except Exception as error:
            result.errors.append(concise_git_message(str(error)))
            return result

    if plan.create_github_repository:
        if tracker:
            tracker.status("creating GitHub repository")
        if not create_github_repo(plan.project_name, github_token, quiet=True):
            result.errors.append("Failed to create the GitHub repository")
            return result
        result.created_repository = True

    try:
        refs = prepare_destination_refs(source, plan.project_name, on_status)
    except Exception as error:
        result.errors.append(concise_git_message(str(error)))
        return result

    if tracker:
        tracker.queue(len(refs.branches), len(refs.tags))

    ref_count = len(refs.branches) + len(refs.tags)
    index = 0
    for branch in refs.branches:
        index += 1
        if tracker:
            tracker.start_ref("branch", branch, index, ref_count)
        try:
            outcome = push_branch(refs, branch, result, tracker.note if tracker else None)
        except Exception as error:
            result.errors.append(f"B {branch}: {concise_git_message(str(error))}")
            outcome = "failed"
        if tracker:
            tracker.finish_ref(outcome)
    for tag in refs.tags:
        index += 1
        if tracker:
            tracker.start_ref("tag", tag, index, ref_count)
        try:
            outcome = push_tag(refs, tag, result, tracker.note if tracker else None)
        except Exception as error:
            result.errors.append(f"T {tag}: {concise_git_message(str(error))}")
            outcome = "failed"
        if tracker:
            tracker.finish_ref(outcome)

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
    stats = CleanMirrorStats(len(plans))
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    repo_task = progress.add_task("[cyan]Mirroring repositories", total=len(plans))
    ref_task = progress.add_task("[cyan]Pushing refs", total=0)

    def run_plan(plan: CleanMirrorPlan) -> CleanMirrorResult:
        tracker = CleanMirrorTracker(stats, progress, ref_task, plan.project_name)
        try:
            result = execute_plan(
                plan,
                top,
                local_repositories,
                github_token,
                tracker,
            )
        except Exception as error:
            result = CleanMirrorResult(
                project_name=plan.project_name,
                errors=[concise_git_message(str(error))],
            )
        stats.finish_repository(
            tracker.activity_id,
            bool(result.errors),
            result.used_cache,
            result.created_repository,
        )
        progress.update(repo_task, advance=1)
        return result

    with Live(console=console, refresh_per_second=4, transient=False) as live:
        def refresh():
            live.update(Group(progress.get_renderable(), render_clean_status(stats)))

        def refresh_loop():
            while not stop_refresh.wait(0.25):
                refresh()

        stop_refresh = threading.Event()
        refresh_thread = threading.Thread(target=refresh_loop)
        refresh_thread.start()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(run_plan, plan): plan
                for plan in plans
            }
            for future in as_completed(futures):
                results.append(future.result())

        stop_refresh.set()
        refresh_thread.join()
        refresh()

    return render_results(results)
