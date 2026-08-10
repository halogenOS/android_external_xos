"""Import smoke tests for every module that moved onto the Forgejo helpers."""

import importlib

import pytest

MIGRATED_MODULES = [
    "clean_mirror",
    "manage_webhooks",
    "merge_branches",
    "merge_upstream",
    "mirror_all",
    "mirror_drift",
    "reticulate_splines",
    "set_default_branches",
    "xos_common",
]


@pytest.mark.parametrize("name", MIGRATED_MODULES)
def test_module_imports(name):
    assert importlib.import_module(name) is not None


def test_clean_mirror_uses_the_forgejo_source_url():
    from clean_mirror import repository_urls
    from xos_common import forgejo_repo_ssh_url

    source, destination = repository_urls("android_frameworks_base")

    assert source == forgejo_repo_ssh_url("android_frameworks_base")
    assert destination.startswith("git@github.com:")
