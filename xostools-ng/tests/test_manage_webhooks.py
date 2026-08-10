"""Tests for the Forgejo webhook reconciliation."""

import manage_webhooks as mw


class FakeProgress:
    def __init__(self):
        self.updates = 0

    def update(self, amount):
        self.updates += amount


class FakeClient:
    def __init__(self, hooks):
        self.hooks = list(hooks)
        self.created = []
        self.deleted = []

    def list_hooks(self, name, owner=None):
        return self.hooks

    def create_hook(self, name, target_url, owner=None, events=None, active=True):
        self.created.append((owner, name, target_url, tuple(events or []), active))
        return {"id": 99}

    def delete_hook(self, name, hook_id, owner=None):
        self.deleted.append((owner, name, hook_id))


def reset_stats():
    for key in mw.stats:
        mw.stats[key] = 0


def run(client, target="https://ci.example/push"):
    reset_stats()
    return mw.process_project_webhooks(
        client, "halogenOS", "android_new", "halogenOS/android_new", target, FakeProgress()
    )


def test_push_webhook_detection():
    assert mw.is_push_webhook("https://ci.example/push")
    assert mw.is_push_webhook("https://ci.example/push/")
    assert not mw.is_push_webhook("https://ci.example/pushed")
    assert not mw.is_push_webhook(None)


def test_delivery_check_reads_active_and_events():
    assert mw.delivers_push_events({"active": True, "events": ["push"]})
    assert not mw.delivers_push_events({"active": False, "events": ["push"]})
    assert not mw.delivers_push_events({"active": True, "events": ["create"]})
    # Unreported fields must not cause delete-and-recreate churn.
    assert mw.delivers_push_events({})


def test_missing_hook_is_created():
    client = FakeClient([])
    result = run(client)

    assert result.status == "added"
    assert client.created == [
        ("halogenOS", "android_new", "https://ci.example/push", ("push",), True)
    ]
    assert mw.stats["added"] == 1


def test_correct_hook_is_left_alone():
    client = FakeClient([
        {"id": 1, "config": {"url": "https://ci.example/push"},
         "active": True, "events": ["push"]},
    ])
    result = run(client)

    assert result.status == "skipped"
    assert client.created == []
    assert client.deleted == []


def test_inactive_hook_on_our_url_is_replaced():
    client = FakeClient([
        {"id": 2, "config": {"url": "https://ci.example/push"},
         "active": False, "events": ["push"]},
    ])
    result = run(client)

    assert result.status == "added"
    assert client.deleted == [("halogenOS", "android_new", 2)]
    assert len(client.created) == 1


def test_hook_without_push_events_is_replaced():
    client = FakeClient([
        {"id": 3, "config": {"url": "https://ci.example/push"},
         "active": True, "events": ["create", "delete"]},
    ])
    result = run(client)

    assert result.status == "added"
    assert client.deleted == [("halogenOS", "android_new", 3)]
    assert len(client.created) == 1


def test_duplicate_hooks_are_reduced_to_one():
    client = FakeClient([
        {"id": 10, "config": {"url": "https://ci.example/push"},
         "active": True, "events": ["push"]},
        {"id": 11, "config": {"url": "https://ci.example/push"},
         "active": True, "events": ["push"]},
    ])
    result = run(client)

    assert result.status == "removed"
    assert client.created == []
    assert client.deleted == [("halogenOS", "android_new", 11)]
    assert result.webhooks_removed == 1


def test_stale_push_hook_is_replaced():
    client = FakeClient([{"id": 4, "config": {"url": "https://old.example/push"}}])
    result = run(client)

    assert result.status == "added"
    assert client.deleted == [("halogenOS", "android_new", 4)]
    assert len(client.created) == 1
    assert result.webhooks_removed == 1


def test_unrelated_hooks_survive():
    client = FakeClient([
        {"id": 7, "config": {"url": "https://ci.example/build"}},
        {"id": 8, "config": {"url": "https://ci.example/push"}},
    ])
    result = run(client)

    assert result.status == "skipped"
    assert client.deleted == []


def test_top_level_url_is_understood():
    client = FakeClient([{"id": 5, "url": "https://ci.example/push"}])
    result = run(client)

    assert result.status == "skipped"
    assert client.created == []


def test_create_failure_is_reported():
    class FailingClient(FakeClient):
        def create_hook(self, *args, **kwargs):
            raise RuntimeError("HTTP 403: forbidden")

    result = run(FailingClient([]))

    assert result.status == "error"
    assert "forbidden" in result.message
    assert mw.stats["errors"] == 1
