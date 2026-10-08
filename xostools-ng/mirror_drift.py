#!/usr/bin/env python3
"""Report repository and ref drift between the XOS Forgejo and GitHub mirrors."""

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from github import Github, GithubException
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
    FORGEJO_OWNER,
    FORGEJO_URL,
    GITHUB_ORG,
    ForgejoClient,
    console,
    get_forgejo_token,
    get_github_token,
    repository_is_empty,
)
from git_credential_prompts import switch_off_git_credential_prompts


MAX_WORKERS = 10
FORGEJO_STYLE = "yellow"
GITHUB_STYLE = "cyan"

# GitHub answers ref listings for a repository without commits with 409 rather
# than with an empty list.
GITHUB_EMPTY_REPOSITORY_STATUS = 409

stop_event = threading.Event()


@dataclass(frozen=True)
class RefState:
    """The Git object stored in a ref and the commit it resolves to."""

    object_sha: str
    commit_sha: str


@dataclass(frozen=True)
class CommitDistance:
    """Commits reachable from only one side of a pair of refs."""

    forgejo_only: int
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
    """Read a commit SHA from a Forgejo branch or tag payload."""
    commit = ref.get("commit") if isinstance(ref, dict) else getattr(ref, "commit", None)
    if isinstance(commit, dict):
        sha = commit.get("id") or commit.get("sha")
    else:
        sha = getattr(commit, "id", None) or getattr(commit, "sha", None)

    if not sha:
        name = ref.get("name") if isinstance(ref, dict) else getattr(ref, "name", None)
        raise ValueError(f"No commit SHA returned for ref {name or '<unknown>'}")
    return sha


def collect_forgejo_refs(
    client: ForgejoClient, owner: str, name: str, empty: bool = False
) -> Tuple[Dict[str, RefState], Dict[str, RefState]]:
    """Collect every branch and tag from a Forgejo repository."""
    if empty:
        # Forgejo serves no ref endpoints for a repository without commits, so
        # asking would report the whole repository as an incomplete comparison.
        return {}, {}

    branches = {}
    for branch in client.list_branches(name, owner=owner):
        commit_sha = ref_commit_sha(branch)
        branches[branch["name"]] = RefState(commit_sha, commit_sha)

    tags = {}
    for tag in client.list_tags(name, owner=owner):
        # Forgejo reports the tag object in `id` and the commit it peels to in
        # `commit.sha`; for a lightweight tag both are the same commit.
        commit_sha = ref_commit_sha(tag)
        object_sha = tag.get("id") or commit_sha
        tags[tag["name"]] = RefState(object_sha, commit_sha)

    return branches, tags


def collect_github_refs(repository: Any) -> Tuple[Dict[str, RefState], Dict[str, RefState]]:
    """Collect every branch and tag from a GitHub repository."""
    branches = {}
    try:
        for branch in repository.get_branches():
            commit_sha = branch.commit.sha
            branches[branch.name] = RefState(commit_sha, commit_sha)

        tag_commits = {tag.name: tag.commit.sha for tag in repository.get_tags()}
        tag_objects = {}
        if tag_commits:
            for ref in repository.get_git_matching_refs("tags/"):
                name = ref.ref.removeprefix("refs/tags/")
                tag_objects[name] = ref.object.sha
    except GithubException as error:
        # A repository the clean mirror has only just created carries no refs
        # to compare, which is a state and not a failure.
        if error.status == GITHUB_EMPTY_REPOSITORY_STATUS:
            return {}, {}
        raise

    tags = {}
    for name in set(tag_commits) | set(tag_objects):
        commit_sha = tag_commits.get(name) or tag_objects[name]
        tags[name] = RefState(tag_objects.get(name, commit_sha), commit_sha)

    return branches, tags


def count_forgejo_commits(
    client: ForgejoClient, owner: str, name: str, from_sha: str, to_sha: str
) -> int:
    """Count commits reachable from to_sha but not from from_sha on Forgejo."""
    comparison = client.compare_commits(name, from_sha, to_sha, owner=owner)
    if not isinstance(comparison, dict):
        raise ComparisonUnavailable("Forgejo returned no comparison")

    total = comparison.get("total_commits")
    if isinstance(total, int):
        return total

    commits = comparison.get("commits")
    if not isinstance(commits, list):
        raise ComparisonUnavailable("Forgejo returned no commit list")
    return len(commits)


def compare_on_forgejo(
    client: ForgejoClient, owner: str, name: str, forgejo_sha: str, github_sha: str
) -> CommitDistance:
    """Compare two commits using objects available in the Forgejo repository."""
    return CommitDistance(
        forgejo_only=count_forgejo_commits(client, owner, name, github_sha, forgejo_sha),
        github_only=count_forgejo_commits(client, owner, name, forgejo_sha, github_sha),
    )


def compare_on_github(repository: Any, forgejo_sha: str, github_sha: str) -> CommitDistance:
    """Compare two commits using objects available in the GitHub repository."""
    comparison = repository.compare(forgejo_sha, github_sha)
    return CommitDistance(
        forgejo_only=int(comparison.behind_by),
        github_only=int(comparison.ahead_by),
    )


def concise_error(error: Exception) -> str:
    """Reduce an API exception to a compact, single-line message."""
    message = " ".join(str(error).split())
    return message[:240] + ("…" if len(message) > 240 else "")


def compare_commits(
    client: ForgejoClient,
    owner: str,
    name: str,
    github_repository: Any,
    forgejo_sha: str,
    github_sha: str,
) -> CommitDistance:
    """Compare commits on the first service that still has both objects."""
    errors = []

    try:
        return compare_on_forgejo(client, owner, name, forgejo_sha, github_sha)
    except Exception as error:
        errors.append(f"Forgejo: {concise_error(error)}")

    try:
        return compare_on_github(github_repository, forgejo_sha, github_sha)
    except Exception as error:
        errors.append(f"GitHub: {concise_error(error)}")

    lowered = " ".join(errors).lower()
    if "no common ancestor" in lowered or "unrelated" in lowered:
        raise ComparisonUnavailable("unrelated histories")
    raise ComparisonUnavailable("; ".join(errors))


def compare_ref_sets(
    kind: str,
    forgejo_refs: Dict[str, RefState],
    github_refs: Dict[str, RefState],
    compare: Callable[[str, str], CommitDistance],
) -> List[RefDrift]:
    """Return only the refs that are missing or differ."""
    drift = []

    for name in sorted(set(forgejo_refs) | set(github_refs), key=str.casefold):
        forgejo_ref = forgejo_refs.get(name)
        github_ref = github_refs.get(name)

        if github_ref is None:
            drift.append(RefDrift(kind, name, location="forgejo"))
            continue
        if forgejo_ref is None:
            drift.append(RefDrift(kind, name, location="github"))
            continue
        if forgejo_ref == github_ref:
            continue

        if forgejo_ref.commit_sha == github_ref.commit_sha:
            drift.append(RefDrift(kind, name, description="different tag object · same commit"))
            continue

        try:
            distance = compare(forgejo_ref.commit_sha, github_ref.commit_sha)
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
            if ref.location == "forgejo":
                tags.append(ref.name)
            continue

        if ref.location == "forgejo":
            branches.append(ref.name)
        elif ref.distance and ref.distance.forgejo_only and not ref.distance.github_only:
            branches.append(ref.name)

    return tuple(branches), tuple(tags)


def compare_repository(
    client: ForgejoClient,
    forgejo_summary: Dict,
    github_repository: Any,
) -> RepositoryDrift:
    """Fetch and compare all refs for one repository."""
    name = forgejo_summary["name"]
    owner = (forgejo_summary.get("owner") or {}).get("login") or FORGEJO_OWNER

    try:
        forgejo_branches, forgejo_tags = collect_forgejo_refs(
            client, owner, name, empty=repository_is_empty(forgejo_summary)
        )
        github_branches, github_tags = collect_github_refs(github_repository)

        distance_cache = {}

        def compare(forgejo_sha: str, github_sha: str) -> CommitDistance:
            key = (forgejo_sha, github_sha)
            if key not in distance_cache:
                distance_cache[key] = compare_commits(
                    client,
                    owner,
                    name,
                    github_repository,
                    forgejo_sha,
                    github_sha,
                )
            return distance_cache[key]

        refs = compare_ref_sets("branch", forgejo_branches, github_branches, compare)
        refs.extend(compare_ref_sets("tag", forgejo_tags, github_tags, compare))
        return RepositoryDrift(name=name, refs=refs)
    except Exception as error:
        return RepositoryDrift(name=name, error=concise_error(error))


def project_index(projects: List[Any], name_of: Callable[[Any], str]) -> Dict[str, Any]:
    """Index projects by their case-insensitive repository name."""
    indexed = {}
    for project in projects:
        name = name_of(project)
        key = name.casefold()
        if key in indexed:
            raise ValueError(f"Duplicate repository name after case folding: {name}")
        indexed[key] = project
    return indexed


def render_project_presence(forgejo_only: List[str], github_only: List[str]):
    """Render repositories that exist on only one service."""
    heading = Text("PROJECTS", style="bold")
    heading.append(f"   FJ-only {len(forgejo_only)}", style=FORGEJO_STYLE)
    heading.append(f"   GH-only {len(github_only)} hidden", style=GITHUB_STYLE)
    console.print(heading)

    for name in forgejo_only:
        line = Text("  FJ  ", style=f"bold {FORGEJO_STYLE}")
        line.append(name)
        console.print(line)


def distance_text(drift: RefDrift) -> Text:
    """Format one compact, color-coded ref status."""
    if drift.location == "forgejo":
        return Text("FJ only", style=FORGEJO_STYLE)
    if drift.description:
        style = "red" if drift.error else "yellow"
        return Text(drift.description, style=style)

    distance = drift.distance
    if distance is None:
        return Text("different", style="red")
    if distance.forgejo_only and not distance.github_only:
        text = Text("GH", style=f"bold {GITHUB_STYLE}")
        text.append(f" behind {distance.forgejo_only}")
        return text
    if distance.github_only and not distance.forgejo_only:
        text = Text("FJ", style=f"bold {FORGEJO_STYLE}")
        text.append(f" behind {distance.github_only}")
        return text
    if not distance.forgejo_only and not distance.github_only:
        return Text("different commits", style="yellow")

    text = Text("FJ", style=f"bold {FORGEJO_STYLE}")
    text.append(f" +{distance.forgejo_only}", style=FORGEJO_STYLE)
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
        description="Report repository, branch, and tag drift between Forgejo and GitHub"
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
    switch_off_git_credential_prompts()
    stop_event.clear()
    args = parse_args()

    if args.workers < 1:
        console.print("[red]Error:[/red] --workers must be at least 1")
        return 1

    forgejo_token = get_forgejo_token()
    github_token = get_github_token()
    if not forgejo_token:
        console.print(
            "[red]Error:[/red] Forgejo token not found; set XOS_FORGEJO_TOKEN "
            "or create ~/.creds/xos_forgejo_token"
        )
        return 1
    if not github_token:
        console.print("[red]Error:[/red] GitHub token not found at ~/.creds/xos_github_token")
        return 1

    try:
        console.print(f"[cyan]Connecting to Forgejo at {FORGEJO_URL}...[/cyan]")
        client = ForgejoClient(token=forgejo_token)
        client.authenticated_user()
        client.get_organization(FORGEJO_OWNER)

        console.print(f"[cyan]Connecting to GitHub organization {GITHUB_ORG}...[/cyan]")
        github = Github(github_token)
        _ = github.get_user().login
        organization = github.get_organization(GITHUB_ORG)

        console.print("[cyan]Discovering repositories...[/cyan]")
        forgejo_projects = client.list_organization_repositories(FORGEJO_OWNER)
        github_repositories = list(organization.get_repos(type="all"))

        forgejo_by_name = project_index(forgejo_projects, lambda project: project["name"])
        github_by_name = project_index(github_repositories, lambda repository: repository.name)
    except Exception as error:
        console.print(f"[red]Error:[/red] {concise_error(error)}")
        return 1

    forgejo_only_keys = sorted(set(forgejo_by_name) - set(github_by_name))
    github_only_keys = sorted(set(github_by_name) - set(forgejo_by_name))
    paired_keys = sorted(set(forgejo_by_name) & set(github_by_name))

    forgejo_only = [forgejo_by_name[key]["name"] for key in forgejo_only_keys]
    github_only = [github_by_name[key].name for key in github_only_keys]
    if forgejo_only or github_only:
        render_project_presence(forgejo_only, github_only)

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
                    client,
                    forgejo_by_name[key],
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
                            name=forgejo_by_name[key]["name"],
                            error=concise_error(error),
                        )
                    )
                progress.update(task, advance=1)

    incomplete = render_repository_drift(results)
    has_drift = bool(forgejo_only or github_only or any(result.refs for result in results))
    mirror_errors = 0

    if args.clean_mirror:
        plans = [
            CleanMirrorPlan(
                project_name=forgejo_by_name[key]["name"],
                all_refs=True,
                create_github_repository=True,
            )
            for key in forgejo_only_keys
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
