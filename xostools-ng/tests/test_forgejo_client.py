"""Tests for the shared Forgejo client and repository URL helpers."""

import os
import threading

import pytest

import xos_common
from xos_common import (
    FORGEJO_MAX_PAGE_LIMIT,
    FORGEJO_OWNER,
    FORGEJO_SSH_BASE,
    FORGEJO_URL,
    ForgejoError,
    forgejo_repo_ssh_url,
    forgejo_repo_url,
    get_forgejo_token,
    hook_target_url,
    repository_display_name,
    repository_is_empty,
)

from conftest import FakeResponse


def json_handler(payload, status_code=200):
    return lambda call: FakeResponse(status_code=status_code, payload=payload)


# -- URL helpers ----------------------------------------------------------


def test_shipped_defaults_point_at_the_xos_service():
    for name in ("XOS_FORGEJO_URL", "XOS_FORGEJO_OWNER", "XOS_FORGEJO_SSH_BASE"):
        if name in os.environ:
            pytest.skip(f"{name} overrides the shipped default")

    assert FORGEJO_URL == "https://git.halogenos.org"
    assert FORGEJO_OWNER == "halogenOS"
    assert FORGEJO_SSH_BASE == "git@git.halogenos.org"


def test_repo_url_uses_configured_defaults():
    assert forgejo_repo_url("android_frameworks_base") == \
        f"{FORGEJO_URL}/{FORGEJO_OWNER}/android_frameworks_base"


def test_repo_url_accepts_overrides_and_trims_separators():
    assert forgejo_repo_url("repo", owner="other", base_url="https://forge.example/") == \
        "https://forge.example/other/repo"


def test_repo_ssh_url_uses_configured_defaults():
    assert forgejo_repo_ssh_url("android_manifest") == \
        f"{FORGEJO_SSH_BASE}:{FORGEJO_OWNER}/android_manifest"


def test_repo_ssh_url_accepts_overrides_and_trims_separators():
    assert forgejo_repo_ssh_url("repo", owner="other", ssh_base="git@forge.example:") == \
        "git@forge.example:other/repo"


# -- Credentials ----------------------------------------------------------


def test_token_prefers_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = tmp_path / ".creds"
    creds.mkdir()
    (creds / "xos_forgejo_token").write_text("from-file\n")
    monkeypatch.setenv("XOS_FORGEJO_TOKEN", " from-env ")

    assert get_forgejo_token() == "from-env"


def test_token_falls_back_to_credentials_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XOS_FORGEJO_TOKEN", raising=False)
    creds = tmp_path / ".creds"
    creds.mkdir()
    (creds / "xos_forgejo_token").write_text("from-file\n")

    assert get_forgejo_token() == "from-file"


def test_token_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XOS_FORGEJO_TOKEN", raising=False)

    assert get_forgejo_token() is None


# -- Transport ------------------------------------------------------------


def test_authentication_header_and_api_prefix(client_for):
    client, fake = client_for(json_handler({"login": "ci"}))

    assert client.authenticated_user() == {"login": "ci"}
    assert fake.calls[0].url == f"{FORGEJO_URL}/api/v1/user"
    assert fake.sessions[0].headers["Authorization"] == "token test-token"


def test_no_authorization_header_without_token(client_for):
    client, fake = client_for(json_handler({"login": "anonymous"}), token=None)
    client.authenticated_user()

    assert "Authorization" not in fake.sessions[0].headers


def test_timeout_is_passed_to_every_request(client_for):
    client, fake = client_for(json_handler({}), timeout=7)
    client.authenticated_user()

    assert fake.calls[0].timeout == 7


def test_timeout_environment_is_bounded(monkeypatch):
    monkeypatch.delenv("XOS_FORGEJO_TIMEOUT", raising=False)
    assert xos_common._forgejo_timeout_from_environment() == 30.0

    monkeypatch.setenv("XOS_FORGEJO_TIMEOUT", "12.5")
    assert xos_common._forgejo_timeout_from_environment() == 12.5

    monkeypatch.setenv("XOS_FORGEJO_TIMEOUT", "not-a-number")
    assert xos_common._forgejo_timeout_from_environment() == 30.0

    monkeypatch.setenv("XOS_FORGEJO_TIMEOUT", "0")
    assert xos_common._forgejo_timeout_from_environment() == 30.0

    monkeypatch.setenv("XOS_FORGEJO_TIMEOUT", "99999")
    assert xos_common._forgejo_timeout_from_environment() == 600.0


def test_non_finite_timeout_falls_back(monkeypatch):
    for raw in ("nan", "inf", "-inf"):
        monkeypatch.setenv("XOS_FORGEJO_TIMEOUT", raw)
        assert xos_common._forgejo_timeout_from_environment() == 30.0


def test_each_thread_gets_its_own_session(client_for):
    client, fake = client_for(json_handler({}))
    seen = []
    barrier = threading.Barrier(3)

    def call():
        barrier.wait()
        client.authenticated_user()
        seen.append(id(client.session))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(seen)) == 3
    assert len(fake.sessions) == 3


def test_session_is_reused_within_one_thread(client_for):
    client, fake = client_for(json_handler({}))
    client.authenticated_user()
    client.authenticated_user()

    assert len(fake.sessions) == 1


# -- Errors ---------------------------------------------------------------


def test_error_carries_status_code_and_message(client_for):
    client, _ = client_for(json_handler({"message": "repo does not exist"}, status_code=404))

    with pytest.raises(ForgejoError) as raised:
        client.get_repository("missing")

    assert raised.value.status_code == 404
    assert "repo does not exist" in str(raised.value)


def test_error_falls_back_to_response_text(client_for):
    client, _ = client_for(lambda call: FakeResponse(status_code=500, text="boom\n  boom"))

    with pytest.raises(ForgejoError) as raised:
        client.get_repository("thing")

    assert str(raised.value) == "HTTP 500: boom boom"


def test_malformed_json_is_reported(client_for):
    client, _ = client_for(lambda call: FakeResponse(status_code=200, malformed=True))

    with pytest.raises(ForgejoError, match="Malformed JSON"):
        client.get_repository("thing")


def test_transport_failure_is_wrapped(client_for):
    def handler(call):
        raise xos_common.requests.RequestException("connection reset")

    client, _ = client_for(handler)

    with pytest.raises(ForgejoError, match="connection reset"):
        client.authenticated_user()


def test_repository_exists_maps_404_to_false(client_for):
    client, _ = client_for(json_handler({"message": "not found"}, status_code=404))
    assert client.repository_exists("missing") is False


def test_repository_exists_propagates_other_errors(client_for):
    client, _ = client_for(json_handler({"message": "nope"}, status_code=403))
    with pytest.raises(ForgejoError):
        client.repository_exists("forbidden")


# -- Pagination -----------------------------------------------------------


def test_pagination_walks_pages_until_a_short_one(client_for):
    pages = {
        1: [{"name": f"repo-{index}"} for index in range(FORGEJO_MAX_PAGE_LIMIT)],
        2: [{"name": "repo-last"}],
    }

    def handler(call):
        return FakeResponse(payload=pages.get(call.params["page"], []))

    client, fake = client_for(handler)
    repositories = client.list_organization_repositories()

    assert len(repositories) == FORGEJO_MAX_PAGE_LIMIT + 1
    assert [call.params["page"] for call in fake.calls] == [1, 2]


def test_pagination_never_exceeds_the_server_limit(client_for):
    client, fake = client_for(json_handler([]))
    client.paginate("orgs/halogenOS/repos", limit=500)

    assert fake.calls[0].params["limit"] == FORGEJO_MAX_PAGE_LIMIT


def test_pagination_unwraps_a_data_envelope(client_for):
    client, _ = client_for(json_handler({"data": [{"name": "one"}]}))

    assert client.list_branches("repo") == [{"name": "one"}]


def test_pagination_rejects_a_non_list_payload(client_for):
    client, _ = client_for(json_handler(42))

    with pytest.raises(ForgejoError, match="Expected a list"):
        client.list_tags("repo")


# -- Endpoints ------------------------------------------------------------


def test_path_segments_are_percent_encoded(client_for):
    client, fake = client_for(json_handler({}))
    client.get_repository("android_device_lineage/sepolicy", owner="ha lo")

    assert fake.calls[0].url.endswith(
        "/api/v1/repos/ha%20lo/android_device_lineage%2Fsepolicy"
    )


def test_create_repository_targets_the_organization(client_for):
    client, fake = client_for(json_handler({"name": "android_new"}))
    client.create_repository("android_new")

    call = fake.calls[0]
    assert call.method == "POST"
    assert call.url.endswith("/api/v1/orgs/halogenOS/repos")
    assert call.json == {"name": "android_new", "private": False, "auto_init": False}


def test_edit_repository_patches_the_given_fields(client_for):
    client, fake = client_for(json_handler({}))
    client.edit_repository("android_new", default_branch="XOS-16.2", private=False)

    call = fake.calls[0]
    assert call.method == "PATCH"
    assert call.url.endswith("/api/v1/repos/halogenOS/android_new")
    assert call.json == {"default_branch": "XOS-16.2", "private": False}


def test_compare_commits_builds_a_basehead_path(client_for):
    client, fake = client_for(json_handler({"total_commits": 3}))
    client.compare_commits("android_new", "aaa", "bbb")

    assert fake.calls[0].url.endswith("/api/v1/repos/halogenOS/android_new/compare/aaa...bbb")


def test_create_hook_uses_the_forgejo_push_hook_shape(client_for):
    client, fake = client_for(json_handler({"id": 1}))
    client.create_hook("android_new", "https://ci.example/push")

    call = fake.calls[0]
    assert call.method == "POST"
    assert call.url.endswith("/api/v1/repos/halogenOS/android_new/hooks")
    assert call.json == {
        "type": "forgejo",
        "config": {"url": "https://ci.example/push", "content_type": "json"},
        "events": ["push"],
        "active": True,
    }


def test_delete_hook_targets_the_hook_id(client_for):
    client, fake = client_for(lambda call: FakeResponse(status_code=204))
    client.delete_hook("android_new", 17)

    call = fake.calls[0]
    assert call.method == "DELETE"
    assert call.url.endswith("/api/v1/repos/halogenOS/android_new/hooks/17")


def test_hook_target_url_reads_config_then_top_level():
    assert hook_target_url({"config": {"url": "https://a/push"}}) == "https://a/push"
    assert hook_target_url({"url": "https://b/push"}) == "https://b/push"
    assert hook_target_url({"config": {}, "url": "https://c/push"}) == "https://c/push"
    assert hook_target_url({}) is None


def test_repository_display_name_prefers_the_full_name():
    assert repository_display_name({"full_name": "halogenOS/x", "name": "x"}) == "halogenOS/x"
    assert repository_display_name({"name": "x"}) == "x"
    assert repository_display_name({}) == "<unknown>"


def test_repository_is_empty_reads_the_forgejo_flag():
    assert repository_is_empty({"empty": True})
    assert not repository_is_empty({"empty": False})
    assert not repository_is_empty({})
