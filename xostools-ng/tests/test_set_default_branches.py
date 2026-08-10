"""Tests for the Forgejo default-branch and visibility pass."""

import threading

import set_default_branches as sdb


class FakeClient:
    """Records the edits a run would apply."""

    def __init__(self, repository, branches):
        self.repository = repository
        self.branches = branches
        self.edits = []

    def get_repository(self, name, owner=None):
        return self.repository

    def list_branches(self, name, owner=None):
        return self.branches

    def edit_repository(self, name, owner=None, **fields):
        self.edits.append((owner, name, fields))
        return {}


def test_branch_names_reads_the_forgejo_payload():
    assert sdb.branch_names([{"name": "XOS-16.2"}, {"name": "main"}, {}]) == \
        ["XOS-16.2", "main"]


def test_latest_xos_branch_sorts_by_version():
    names = ["main", "XOS-16.0", "XOS-16.2", "XOS-16.0.1", "lineage-23.2"]
    assert sdb.select_latest_xos_branch(names) == "XOS-16.2"


def test_latest_xos_branch_without_candidates():
    assert sdb.select_latest_xos_branch(["main", "16-qpr2"]) is None


def test_default_branch_and_visibility_are_corrected():
    client = FakeClient(
        {"default_branch": "XOS-16.0", "private": True},
        [{"name": "XOS-16.0"}, {"name": "XOS-16.2"}],
    )

    result = sdb.process_project(client, "halogenOS", "android_new", "halogenOS/android_new")

    assert result.status == "updated"
    assert client.edits == [
        ("halogenOS", "android_new", {"private": False, "default_branch": "XOS-16.2"})
    ]
    assert "XOS-16.0 → XOS-16.2" in result.message
    assert "private → public" in result.message


def test_private_repository_without_xos_branch_is_only_made_public():
    client = FakeClient({"default_branch": "main", "private": True}, [{"name": "main"}])

    result = sdb.process_project(client, "halogenOS", "android_new", "halogenOS/android_new")

    assert result.status == "updated"
    assert client.edits == [("halogenOS", "android_new", {"private": False})]


def test_correct_repository_is_skipped():
    client = FakeClient(
        {"default_branch": "XOS-16.2", "private": False},
        [{"name": "XOS-16.2"}],
    )

    result = sdb.process_project(client, "halogenOS", "android_new", "halogenOS/android_new")

    assert result.status == "skipped"
    assert client.edits == []


def test_empty_repository_is_made_public_without_listing_branches():
    class NoBranchEndpoint(FakeClient):
        def list_branches(self, name, owner=None):
            raise RuntimeError("HTTP 404: The target couldn't be found.")

    client = NoBranchEndpoint(
        {"default_branch": "", "private": True, "empty": True}, []
    )

    result = sdb.process_project(client, "halogenOS", "android_new", "halogenOS/android_new")

    assert result.status == "updated"
    assert client.edits == [("halogenOS", "android_new", {"private": False})]


def test_cancellation_stops_before_the_repository_is_changed():
    cancelled = threading.Event()
    cancelled.set()

    client = FakeClient(
        {"default_branch": "XOS-16.0", "private": True}, [{"name": "XOS-16.2"}]
    )

    result = sdb.process_project(
        client, "halogenOS", "android_new", "halogenOS/android_new",
        cancel_event=cancelled
    )

    assert result.status == "skipped"
    assert result.message == "Cancelled"
    assert client.edits == []


def test_cancellation_between_reading_and_writing_skips_the_edit():
    cancelled = threading.Event()

    class CancellingClient(FakeClient):
        def list_branches(self, name, owner=None):
            cancelled.set()
            return self.branches

    client = CancellingClient(
        {"default_branch": "XOS-16.0", "private": True}, [{"name": "XOS-16.2"}]
    )

    result = sdb.process_project(
        client, "halogenOS", "android_new", "halogenOS/android_new",
        cancel_event=cancelled
    )

    assert result.status == "skipped"
    assert client.edits == []


def test_api_failure_becomes_an_error_result():
    class FailingClient(FakeClient):
        def list_branches(self, name, owner=None):
            raise RuntimeError("HTTP 500: boom")

    client = FailingClient({"default_branch": "main", "private": False}, [])
    result = sdb.process_project(client, "halogenOS", "android_new", "halogenOS/android_new")

    assert result.status == "error"
    assert "boom" in result.message
