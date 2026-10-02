"""GitOperations.unshallow_repo against real repositories on the local file system."""

from pathlib import Path

import git

from xos_common import GitOperations


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    repo = git.Repo.init(source)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Test")
        config.set_value("user", "email", "test@example.com")
    for number in range(3):
        repo.git.commit("--allow-empty", "-m", f"commit {number}")
    return source


def make_shallow_checkout(tmp_path: Path, source: Path) -> Path:
    """A depth-one clone shaped like a repo tool checkout: its remote is not
    origin, HEAD is detached, and a second remote exists that has no history."""
    checkout = tmp_path / "checkout"
    git.Git().clone("--depth", "1", source.as_uri(), str(checkout))
    repo = git.Repo(checkout)
    repo.git.remote("rename", "origin", "aosp")
    repo.git.checkout("--detach")
    repo.git.remote("add", "XOS", (tmp_path / "missing").as_uri())
    return checkout


def test_unshallow_names_the_remote_it_fetches_from(tmp_path):
    checkout = make_shallow_checkout(tmp_path, make_source(tmp_path))
    assert GitOperations.is_shallow_repo(checkout)

    assert GitOperations.unshallow_repo(checkout, ["aosp"])

    assert not GitOperations.is_shallow_repo(checkout)
    assert git.Repo(checkout).git.rev_list("--count", "HEAD") == "3"


def test_unshallow_tries_every_remote_by_default(tmp_path):
    checkout = make_shallow_checkout(tmp_path, make_source(tmp_path))

    assert GitOperations.unshallow_repo(checkout)

    assert not GitOperations.is_shallow_repo(checkout)


def test_unshallow_fails_when_no_remote_completes_the_repository(tmp_path):
    checkout = make_shallow_checkout(tmp_path, make_source(tmp_path))

    assert not GitOperations.unshallow_repo(checkout, ["XOS"])

    assert GitOperations.is_shallow_repo(checkout)


def test_a_complete_repository_needs_nothing(tmp_path):
    source = make_source(tmp_path)

    assert GitOperations.unshallow_repo(source, [])
