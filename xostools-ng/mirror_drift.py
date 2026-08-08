#!/usr/bin/env python3
"""Report repository and ref drift between the XOS GitLab and GitHub mirrors."""

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import gitlab
from github import Github
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from clean_mirror import CleanMirrorPlan, mirror_clean_plans
from xos_common import (
    GITHUB_ORG,
    GITLAB_GROUP_ID,
    GITLAB_URL,
    console,
    get_github_token,
    get_gitlab_token,
)


MAX_WORKERS = 10
GITLAB_STYLE = "yellow"
GITHUB_STYLE = "cyan"

stop_event = threading.Event()


@dataclass(frozen=True)
class RefState:
    """The Git object stored in a ref and the commit it resolves to."""

    object_sha: str
    commit_sha: str


@dataclass(frozen=True)
class CommitDistance:
    """Commits reachable from only one side of a pair of refs."""

    gitlab_only: int
    github_only: int


@dataclass(frozen=True)
class RefDrift:
    """A branch or tag that differs between the two services."""

    kind: str
    name: str
    location: Optional[str] = None
    distance: Optional[CommitDistance] = None
    description: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RepositoryDrift:
    """All detected drift for one repository."""

    name: str
    refs: List[RefDrift] = field(default_factory=list)
    error: Optional[str] = None


class ComparisonUnavailable(RuntimeError):
    """Raised when neither service can compare both commit objects."""


def ref_commit_sha(ref: Any) -> str:
    """Read a commit SHA from a GitLab branch or tag object."""
    commit = getattr(ref, "commit", None)
    if isinstance(commit, dict):
        sha = commit.get("id") or commit.get("sha")
    else:
        sha = getattr(commit, "id", None) or getattr(commit, "sha", None)

    if not sha:
        raise ValueError(f"No commit SHA returned for ref {getattr(ref, 'name', '<unknown>')}")
    return sha


def collect_gitlab_refs(project: Any) -> Tuple[Dict[str, RefState], Dict[str, RefState]]:
    """Collect every branch and tag from a GitLab project."""
    branches = {}
    for branch in project.branches.list(iterator=True):
        commit_sha = ref_commit_sha(branch)
        branches[branch.name] = RefState(commit_sha, commit_sha)

    tags = {}
    for tag in project.tags.list(iterator=True):
        commit_sha = ref_commit_sha(tag)
        object_sha = getattr(tag, "target", None) or commit_sha
        tags[tag.name] = RefState(object_sha, commit_sha)

    return branches, tags


def collect_github_refs(repository: Any) -> Tuple[Dict[str, RefState], Dict[str, RefState]]:
    """Collect every branch and tag from a GitHub repository."""
    branches = {}
    for branch in repository.get_branches():
        commit_sha = branch.commit.sha
        branches[branch.name] = RefState(commit_sha, commit_sha)

    tag_commits = {tag.name: tag.commit.sha for tag in repository.get_tags()}
    tag_objects = {}
    if tag_commits:
        for ref in repository.get_git_matching_refs("tags/"):
            name = ref.ref.removeprefix("refs/tags/")
            tag_objects[name] = ref.object.sha

    tags = {}
    for name in set(tag_commits) | set(tag_objects):
        commit_sha = tag_commits.get(name) or tag_objects[name]
        tags[name] = RefState(tag_objects.get(name, commit_sha), commit_sha)

    return branches, tags


def count_gitlab_commits(project: Any, from_sha: str, to_sha: str) -> int:
    """Count commits reachable from to_sha but not from from_sha on GitLab."""
    comparison = project.repository_compare(from_sha, to_sha, straight=True)
    commits = comparison.get("commits")
    if not isinstance(commits, list):
        raise ComparisonUnavailable("GitLab returned no commit list")
    return len(commits)


def compare_on_gitlab(project: Any, gitlab_sha: str, github_sha: str) -> CommitDistance:
    """Compare two commits using objects available in the GitLab repository."""
    return CommitDistance(
        gitlab_only=count_gitlab_commits(project, github_sha, gitlab_sha),
        github_only=count_gitlab_commits(project, gitlab_sha, github_sha),
    )


def compare_on_github(repository: Any, gitlab_sha: str, github_sha: str) -> CommitDistance:
    """Compare two commits using objects available in the GitHub repository."""
    comparison = repository.compare(gitlab_sha, github_sha)
    return CommitDistance(
        gitlab_only=int(comparison.behind_by),
        github_only=int(comparison.ahead_by),
    )


def concise_error(error: Exception) -> str:
    """Reduce an API exception to a compact, single-line message."""
    message = " ".join(str(error).split())
    return message[:240] + ("…" if len(message) > 240 else "")


def compare_commits(
    gitlab_project: Any,
    github_repository: Any,
    gitlab_sha: str,
    github_sha: str,
) -> CommitDistance:
    """Compare commits on the first service that still has both objects."""
    errors = []

    try:
        return compare_on_gitlab(gitlab_project, gitlab_sha, github_sha)
    except Exception as error:
        errors.append(f"GitLab: {concise_error(error)}")

    try:
        return compare_on_github(github_repository, gitlab_sha, github_sha)
    except Exception as error:
        errors.append(f"GitHub: {concise_error(error)}")

    lowered = " ".join(errors).lower()
    if "no common ancestor" in lowered or "unrelated" in lowered:
        raise ComparisonUnavailable("unrelated histories")
    raise ComparisonUnavailable("; ".join(errors))


def compare_ref_sets(
    kind: str,
    gitlab_refs: Dict[str, RefState],
    github_refs: Dict[str, RefState],
    compare: Callable[[str, str], CommitDistance],
) -> List[RefDrift]:
    """Return only the refs that are missing or differ."""
    drift = []

    for name in sorted(set(gitlab_refs) | set(github_refs), key=str.casefold):
        gitlab_ref = gitlab_refs.get(name)
        github_ref = github_refs.get(name)

        if github_ref is None:
            drift.append(RefDrift(kind, name, location="gitlab"))
            continue
        if gitlab_ref is None:
            drift.append(RefDrift(kind, name, location="github"))
            continue
        if gitlab_ref == github_ref:
            continue

        if gitlab_ref.commit_sha == github_ref.commit_sha:
            drift.append(RefDrift(kind, name, description="different tag object · same commit"))
            continue

        try:
            distance = compare(gitlab_ref.commit_sha, github_ref.commit_sha)
            drift.append(RefDrift(kind, name, distance=distance))
        except ComparisonUnavailable as error:
            description = "unrelated" if str(error) == "unrelated histories" else "comparison unavailable"
            drift.append(RefDrift(kind, name, description=description, error=str(error)))

    return drift


def clean_mirror_candidates(refs: List[RefDrift]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Select refs that can be mirrored without discarding GitHub history."""
    branches = []
    tags = []

    for ref in refs:
        if ref.kind == "tag":
            if ref.location == "gitlab":
                tags.append(ref.name)
            continue

        if ref.location == "gitlab":
            branches.append(ref.name)
        elif ref.distance and ref.distance.gitlab_only and not ref.distance.github_only:
            branches.append(ref.name)

    return tuple(branches), tuple(tags)


def compare_repository(
    gl: gitlab.Gitlab,
    gitlab_summary: Any,
    github_repository: Any,
) -> RepositoryDrift:
    """Fetch and compare all refs for one repository."""
    name = gitlab_summary.path

    try:
        gitlab_project = gl.projects.get(gitlab_summary.id)
        gitlab_branches, gitlab_tags = collect_gitlab_refs(gitlab_project)
        github_branches, github_tags = collect_github_refs(github_repository)

        distance_cache = {}

        def compare(gitlab_sha: str, github_sha: str) -> CommitDistance:
            key = (gitlab_sha, github_sha)
            if key not in distance_cache:
                distance_cache[key] = compare_commits(
                    gitlab_project,
                    github_repository,
                    gitlab_sha,
                    github_sha,
                )
            return distance_cache[key]

        refs = compare_ref_sets("branch", gitlab_branches, github_branches, compare)
        refs.extend(compare_ref_sets("tag", gitlab_tags, github_tags, compare))
        return RepositoryDrift(name=name, refs=refs)
    except Exception as error:
        return RepositoryDrift(name=name, error=concise_error(error))


def project_index(projects: List[Any], name_attribute: str) -> Dict[str, Any]:
    """Index projects by their case-insensitive repository name."""
    indexed = {}
    for project in projects:
        name = getattr(project, name_attribute)
        key = name.casefold()
        if key in indexed:
            raise ValueError(f"Duplicate repository name after case folding: {name}")
        indexed[key] = project
    return indexed


def render_project_presence(gitlab_only: List[str], github_only: List[str]):
    """Render repositories that exist on only one service."""
    heading = Text("PROJECTS", style="bold")
    heading.append(f"   GL-only {len(gitlab_only)}", style=GITLAB_STYLE)
    heading.append(f"   GH-only {len(github_only)} hidden", style=GITHUB_STYLE)
    console.print(heading)

    for name in gitlab_only:
        line = Text("  GL  ", style=f"bold {GITLAB_STYLE}")
        line.append(name)
        console.print(line)


def distance_text(drift: RefDrift) -> Text:
    """Format one compact, color-coded ref status."""
    if drift.location == "gitlab":
        return Text("GL only", style=GITLAB_STYLE)
    if drift.description:
        style = "red" if drift.error else "yellow"
        return Text(drift.description, style=style)

    distance = drift.distance
    if distance is None:
        return Text("different", style="red")
    if distance.gitlab_only and not distance.github_only:
        text = Text("GH", style=f"bold {GITHUB_STYLE}")
        text.append(f" behind {distance.gitlab_only}")
        return text
    if distance.github_only and not distance.gitlab_only:
        text = Text("GL", style=f"bold {GITLAB_STYLE}")
        text.append(f" behind {distance.github_only}")
        return text
    if not distance.gitlab_only and not distance.github_only:
        return Text("different commits", style="yellow")

    text = Text("GL", style=f"bold {GITLAB_STYLE}")
    text.append(f" +{distance.gitlab_only}", style=GITLAB_STYLE)
    text.append(" · ", style="dim")
    text.append("GH", style=f"bold {GITHUB_STYLE}")
    text.append(f" +{distance.github_only}", style=GITHUB_STYLE)
    return text


def render_repository_drift(results: List[RepositoryDrift]):
    """Render repositories with ref drift and any incomplete comparisons."""
    visible_refs = {
        result.name: [ref for ref in result.refs if ref.location != "github"]
        for result in results
    }
    drifted = [result for result in results if visible_refs[result.name]]
    ref_count = sum(len(visible_refs[result.name]) for result in drifted)
    github_only_count = sum(
        ref.location == "github" for result in results for ref in result.refs
    )
    error_count = sum(bool(result.error) for result in results)
    error_count += sum(bool(ref.error) for result in results for ref in result.refs)

    if drifted or github_only_count:
        console.print()
        heading = Text("REF DRIFT", style="bold")
        heading.append(f"   {len(drifted)} projects · {ref_count} refs")
        if github_only_count:
            heading.append(f" · GH-only {github_only_count} hidden", style=GITHUB_STYLE)
        if error_count:
            heading.append(f" · {error_count} incomplete", style="red")
        console.print(heading)

        for result in sorted(drifted, key=lambda item: item.name.casefold()):
            console.print(Text(result.name, style="bold white"))
            table = Table.grid(padding=(0, 1))
            table.add_column(width=2, no_wrap=True, style="dim")
            table.add_column(no_wrap=True)
            table.add_column()

            for drift in visible_refs[result.name]:
                marker = "B" if drift.kind == "branch" else "T"
                table.add_row(marker, drift.name, distance_text(drift))
            console.print(table)

    errors = []
    for result in results:
        if result.error:
            errors.append((result.name, result.error))
        for ref in result.refs:
            if ref.error:
                marker = "B" if ref.kind == "branch" else "T"
                errors.append((f"{result.name} {marker} {ref.name}", ref.error))

    if errors:
        console.print("\n[bold red]INCOMPLETE[/bold red]")
        for name, error in sorted(errors, key=lambda item: item[0].casefold()):
            console.print(Text(f"  {name}: {error}", style="red"))

    return error_count


def parse_args():
    parser = argparse.ArgumentParser(
        description="Report repository, branch, and tag drift between GitLab and GitHub"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"Number of repositories to compare concurrently (default: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--clean-mirror",
        action="store_true",
        help="Mirror refs that are missing from or strictly ahead of GitHub",
    )
    return parser.parse_args()


def main():
    """Discover both namespaces and report every difference."""
    stop_event.clear()
    args = parse_args()

    if args.workers < 1:
        console.print("[red]Error:[/red] --workers must be at least 1")
        return 1

    gitlab_token = get_gitlab_token()
    github_token = get_github_token()
    if not gitlab_token:
        console.print("[red]Error:[/red] GitLab token not found at ~/.creds/xos_gitlab_token")
        return 1
    if not github_token:
        console.print("[red]Error:[/red] GitHub token not found at ~/.creds/xos_github_token")
        return 1

    try:
        console.print(f"[cyan]Connecting to GitLab at {GITLAB_URL}...[/cyan]")
        gl = gitlab.Gitlab(GITLAB_URL, private_token=gitlab_token)
        gl.auth()
        group = gl.groups.get(GITLAB_GROUP_ID)

        console.print(f"[cyan]Connecting to GitHub organization {GITHUB_ORG}...[/cyan]")
        github = Github(github_token)
        _ = github.get_user().login
        organization = github.get_organization(GITHUB_ORG)

        console.print("[cyan]Discovering repositories...[/cyan]")
        gitlab_projects = list(
            group.projects.list(include_subgroups=False, with_shared=False, iterator=True)
        )
        github_repositories = list(organization.get_repos(type="all"))

        gitlab_by_name = project_index(gitlab_projects, "path")
        github_by_name = project_index(github_repositories, "name")
    except Exception as error:
        console.print(f"[red]Error:[/red] {concise_error(error)}")
        return 1

    gitlab_only_keys = sorted(set(gitlab_by_name) - set(github_by_name))
    github_only_keys = sorted(set(github_by_name) - set(gitlab_by_name))
    paired_keys = sorted(set(gitlab_by_name) & set(github_by_name))

    gitlab_only = [gitlab_by_name[key].path for key in gitlab_only_keys]
    github_only = [github_by_name[key].name for key in github_only_keys]
    if gitlab_only or github_only:
        render_project_presence(gitlab_only, github_only)

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
        task = progress.add_task("[cyan]Comparing repositories", total=len(paired_keys))

        with ThreadPoolExecutor(max_workers=args.workers) as executor_pool:
            futures = {
                executor_pool.submit(
                    compare_repository,
                    gl,
                    gitlab_by_name[key],
                    github_by_name[key],
                ): key
                for key in paired_keys
            }

            for future in as_completed(futures):
                key = futures[future]
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append(
                        RepositoryDrift(
                            name=gitlab_by_name[key].path,
                            error=concise_error(error),
                        )
                    )
                progress.update(task, advance=1)

    incomplete = render_repository_drift(results)
    has_drift = bool(gitlab_only or github_only or any(result.refs for result in results))
    mirror_errors = 0

    if args.clean_mirror:
        plans = [
            CleanMirrorPlan(
                project_name=gitlab_by_name[key].path,
                all_refs=True,
                create_github_repository=True,
            )
            for key in gitlab_only_keys
        ]

        for result in results:
            branches, tags = clean_mirror_candidates(result.refs)
            if branches or tags:
                plans.append(
                    CleanMirrorPlan(
                        project_name=result.name,
                        branches=branches,
                        tags=tags,
                    )
                )

        try:
            mirror_errors = mirror_clean_plans(
                plans, github_token, args.workers, stop_event
            )
        except Exception as error:
            console.print(f"\n[red]Clean mirror failed:[/red] {concise_error(error)}")
            mirror_errors = 1
    elif not has_drift and not incomplete:
        console.print("\n[bold green]No drift found.[/bold green]")
    elif not incomplete:
        console.print("\n[bold yellow]Drift found.[/bold yellow]")

    return 1 if incomplete or mirror_errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        stop_event.set()
        console.print("\n[bold red]Interrupted.[/bold red]")
        sys.exit(130)
