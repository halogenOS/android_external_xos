"""Tests for the Forgejo side of the mirror drift report."""

import pytest
from github import GithubException

import mirror_drift as md


class FakeClient:
    def __init__(self, branches=(), tags=(), comparisons=None):
        self._branches = list(branches)
        self._tags = list(tags)
        self._comparisons = comparisons or {}
        self.compared = []

    def list_branches(self, name, owner=None):
        return self._branches

    def list_tags(self, name, owner=None):
        return self._tags

    def compare_commits(self, name, base, head, owner=None):
        self.compared.append((base, head))
        try:
            return self._comparisons[(base, head)]
        except KeyError:
            raise RuntimeError("HTTP 404: object does not exist")


def test_ref_commit_sha_reads_branch_and_tag_shapes():
    assert md.ref_commit_sha({"name": "XOS-16.2", "commit": {"id": "aaa"}}) == "aaa"
    assert md.ref_commit_sha({"name": "v1", "commit": {"sha": "bbb"}}) == "bbb"

    with pytest.raises(ValueError):
        md.ref_commit_sha({"name": "broken", "commit": {}})


def test_annotated_tag_keeps_object_and_commit_apart():
    client = FakeClient(
        branches=[{"name": "XOS-16.2", "commit": {"id": "c0"}}],
        tags=[
            {"name": "annotated", "id": "t0", "commit": {"sha": "c1"}},
            {"name": "lightweight", "commit": {"sha": "c2"}},
        ],
    )

    branches, tags = md.collect_forgejo_refs(client, "halogenOS", "android_new")

    assert branches == {"XOS-16.2": md.RefState("c0", "c0")}
    assert tags["annotated"] == md.RefState("t0", "c1")
    assert tags["lightweight"] == md.RefState("c2", "c2")


def test_commit_count_prefers_the_reported_total():
    client = FakeClient(comparisons={("a", "b"): {"total_commits": 5, "commits": [{}]}})
    assert md.count_forgejo_commits(client, "halogenOS", "android_new", "a", "b") == 5


def test_commit_count_falls_back_to_the_commit_list():
    client = FakeClient(comparisons={("a", "b"): {"commits": [{}, {}]}})
    assert md.count_forgejo_commits(client, "halogenOS", "android_new", "a", "b") == 2


def test_commit_count_without_a_list_is_unavailable():
    client = FakeClient(comparisons={("a", "b"): {}})
    with pytest.raises(md.ComparisonUnavailable):
        md.count_forgejo_commits(client, "halogenOS", "android_new", "a", "b")


def test_compare_counts_both_directions():
    client = FakeClient(
        comparisons={
            ("gh", "fj"): {"total_commits": 3},
            ("fj", "gh"): {"total_commits": 0},
        }
    )

    distance = md.compare_on_forgejo(client, "halogenOS", "android_new", "fj", "gh")

    assert distance == md.CommitDistance(forgejo_only=3, github_only=0)


def test_compare_ref_sets_reports_each_side():
    forgejo = {"only-fj": md.RefState("a", "a"), "both": md.RefState("b", "b")}
    github = {"only-gh": md.RefState("c", "c"), "both": md.RefState("d", "d")}

    drift = md.compare_ref_sets(
        "branch", forgejo, github,
        lambda left, right: md.CommitDistance(forgejo_only=2, github_only=0),
    )

    by_name = {ref.name: ref for ref in drift}
    assert by_name["only-fj"].location == "forgejo"
    assert by_name["only-gh"].location == "github"
    assert by_name["both"].distance == md.CommitDistance(forgejo_only=2, github_only=0)


def test_same_commit_different_tag_object_is_described():
    drift = md.compare_ref_sets(
        "tag",
        {"v1": md.RefState("tagobj", "commit")},
        {"v1": md.RefState("commit", "commit")},
        lambda left, right: pytest.fail("comparison must not be needed"),
    )

    assert drift[0].description == "different tag object · same commit"


def test_clean_mirror_candidates_only_take_safe_refs():
    refs = [
        md.RefDrift("branch", "missing-on-github", location="forgejo"),
        md.RefDrift("branch", "ahead", distance=md.CommitDistance(2, 0)),
        md.RefDrift("branch", "diverged", distance=md.CommitDistance(2, 1)),
        md.RefDrift("branch", "behind", distance=md.CommitDistance(0, 3)),
        md.RefDrift("tag", "new-tag", location="forgejo"),
        md.RefDrift("tag", "github-tag", location="github"),
    ]

    branches, tags = md.clean_mirror_candidates(refs)

    assert branches == ("missing-on-github", "ahead")
    assert tags == ("new-tag",)


def test_distance_text_uses_compact_forgejo_labels():
    assert md.distance_text(md.RefDrift("branch", "x", location="forgejo")).plain == "FJ only"
    assert md.distance_text(
        md.RefDrift("branch", "x", distance=md.CommitDistance(4, 0))
    ).plain == "GH behind 4"
    assert md.distance_text(
        md.RefDrift("branch", "x", distance=md.CommitDistance(0, 2))
    ).plain == "FJ behind 2"
    assert md.distance_text(
        md.RefDrift("branch", "x", distance=md.CommitDistance(1, 2))
    ).plain == "FJ +1 · GH +2"


def test_project_index_rejects_case_folded_duplicates():
    with pytest.raises(ValueError):
        md.project_index([{"name": "Repo"}, {"name": "repo"}], lambda item: item["name"])


def test_empty_repositories_compare_without_asking_for_refs():
    class NoRefEndpoints(FakeClient):
        def list_branches(self, name, owner=None):
            raise RuntimeError("HTTP 404: The target couldn't be found.")

        def list_tags(self, name, owner=None):
            raise RuntimeError("HTTP 404: The target couldn't be found.")

    class EmptyGithubRepository:
        def get_branches(self):
            raise GithubException(409, {"message": "Git Repository is empty."}, None)

        def get_tags(self):
            raise AssertionError("tags must not be requested after the 409")

        def get_git_matching_refs(self, prefix):
            raise AssertionError("refs must not be requested after the 409")

    drift = md.compare_repository(
        NoRefEndpoints(),
        {"name": "android_new", "owner": {"login": "halogenOS"}, "empty": True},
        EmptyGithubRepository(),
    )

    assert drift.error is None
    assert drift.refs == []


def test_compare_repository_reports_ref_drift():
    client = FakeClient(
        branches=[{"name": "XOS-16.2", "commit": {"id": "fj"}}],
        tags=[],
        comparisons={("gh", "fj"): {"total_commits": 1}, ("fj", "gh"): {"total_commits": 0}},
    )

    class GithubBranch:
        name = "XOS-16.2"
        commit = type("Commit", (), {"sha": "gh"})()

    class GithubRepository:
        def get_branches(self):
            return [GithubBranch()]

        def get_tags(self):
            return []

        def get_git_matching_refs(self, prefix):
            return []

    drift = md.compare_repository(
        client, {"name": "android_new", "owner": {"login": "halogenOS"}}, GithubRepository()
    )

    assert drift.error is None
    assert drift.refs[0].distance == md.CommitDistance(forgejo_only=1, github_only=0)
