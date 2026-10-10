"""Repository identity tests: URL normalization, slug, key, registration."""

import json

import pytest

from skill_stats.identity import (
    local_identity,
    normalize_clone_url,
    register_repository,
    repo_id_for,
    repo_key,
    slug_from_clone_url,
)


def test_normalize_clone_url_maps_ssh_to_https():
    assert normalize_clone_url("git@github.com:dahep/dotfiles.git") == (
        "https://github.com/dahep/dotfiles"
    )
    assert normalize_clone_url("ssh://git@github.com/dahep/dotfiles.git") == (
        "https://github.com/dahep/dotfiles"
    )
    assert normalize_clone_url("https://github.com/dahep/dotfiles.git") == (
        "https://github.com/dahep/dotfiles"
    )
    assert normalize_clone_url("http://GitHub.com/dahep/dotfiles") == (
        "https://github.com/dahep/dotfiles"
    )


def test_normalize_clone_url_never_treats_local_paths_as_identity():
    assert normalize_clone_url("/home/tester/dotfiles") is None
    assert normalize_clone_url("../dotfiles") is None
    assert normalize_clone_url("file:///home/tester/dotfiles") is None
    assert normalize_clone_url("") is None


def test_slug_and_key():
    assert slug_from_clone_url("https://github.com/dahep/dotfiles") == "dahep-dotfiles"
    assert local_identity("/home/tester/dotfiles") == ("local:dotfiles", "dotfiles")
    assert repo_key("https://github.com/dahep/dotfiles", "main") == (
        "https://github.com/dahep/dotfiles@main"
    )
    assert repo_key("local:dotfiles", "main") == "local:dotfiles@main"


def test_register_repository_prefers_origin_url(repo, tmp_path):
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:dahep/dotfiles.git"],
        check=True,
        capture_output=True,
    )
    from skill_stats.db import connect

    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    row = conn.execute("SELECT * FROM repositories WHERE id = ?", (rid,)).fetchone()
    assert row["repo_key"] == "https://github.com/dahep/dotfiles@main"
    assert row["slug"] == "dahep-dotfiles"
    assert row["clone_url"] == "https://github.com/dahep/dotfiles"
    assert (
        json.loads(
            conn.execute("SELECT value FROM settings WHERE key = 'repo_id'").fetchone()["value"]
        )
        == rid
    )
    assert repo_id_for(conn) == rid
    # re-registration is idempotent and updates the same single row
    assert register_repository(conn, repo, "main") == rid
    assert conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 1


def test_register_repository_shim_without_origin(repo, tmp_path):
    from skill_stats.db import connect

    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    row = conn.execute("SELECT * FROM repositories WHERE id = ?", (rid,)).fetchone()
    assert row["repo_key"] == "local:repo@main"
    assert row["slug"] == "repo"
    assert row["clone_url"] is None


def test_repo_id_for_requires_registration(tmp_path):
    from skill_stats.db import connect

    conn = connect(tmp_path / "i.db")
    with pytest.raises(RuntimeError, match="run init first"):
        repo_id_for(conn)
