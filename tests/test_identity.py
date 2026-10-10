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


# ------------------------------------------------ re-init safety (item 4) --
# Re-init must never silently relabel existing analysis:
# (a) same key -> idempotent no-op; (b) different key + no data -> replace with
# warning; (c) different key + data -> hard error; (d) never downgrade a
# canonical key to the local: shim when origin resolution fails this run.


def _set_origin(repo, url):
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "remote", "remove", "origin"], check=False, capture_output=True
    )
    if url:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", url],
            check=True,
            capture_output=True,
        )


def _add_commit_row(conn, rid, walk_index=1):
    conn.execute(
        "INSERT INTO commits (repo_id, sha, tree_sha, committed_at, author_name, title,"
        " message, walk_index, kind, diff) VALUES (?, ?, 't', 'd', 'a', 't', 'm', ?,"
        " 'normal', '+x\\n')",
        (rid, f"{walk_index:040d}", walk_index),
    )
    conn.commit()


def test_reregister_same_key_is_idempotent_noop(repo, tmp_path):
    import warnings

    from skill_stats.db import connect

    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        assert register_repository(conn, repo, "main") == rid
    assert not record  # no warning on the no-op path
    assert conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 1


def test_reregister_replaces_when_no_data(repo, tmp_path):
    from skill_stats.db import connect

    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    _set_origin(repo, "git@github.com:dahep/other.git")
    with pytest.warns(UserWarning, match="replacing repository identity"):
        rid2 = register_repository(conn, repo, "main")
    assert rid2 == rid
    row = conn.execute("SELECT repo_key, slug FROM repositories").fetchone()
    assert row["repo_key"] == "https://github.com/dahep/other@main"
    assert row["slug"] == "dahep-other"


def test_reregister_refuses_relabel_with_data(repo, tmp_path):
    from skill_stats.db import connect

    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    _add_commit_row(conn, rid)
    _set_origin(repo, "git@github.com:dahep/other.git")
    with pytest.raises(RuntimeError, match="refusing to relabel"):
        register_repository(conn, repo, "main")
    row = conn.execute("SELECT repo_key FROM repositories").fetchone()
    assert row["repo_key"] == "https://github.com/dahep/dotfiles@main"  # untouched


def test_reregister_refuses_branch_relabel_with_data(repo, tmp_path):
    from skill_stats.db import connect

    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    _add_commit_row(conn, rid)
    with pytest.raises(RuntimeError, match="refusing to relabel"):
        register_repository(conn, repo, "dev")


def test_reregister_never_downgrades_canonical_to_shim(repo, tmp_path):
    from skill_stats.db import connect

    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")
    _add_commit_row(conn, rid)
    _set_origin(repo, None)  # origin resolution fails this run
    with pytest.warns(UserWarning, match="keeping stored repository identity"):
        assert register_repository(conn, repo, "main") == rid
    row = conn.execute("SELECT repo_key FROM repositories").fetchone()
    assert row["repo_key"] == "https://github.com/dahep/dotfiles@main"  # kept, not a shim


def test_reregister_upgrades_shim_to_canonical_with_data(repo, tmp_path):
    from skill_stats.db import connect

    _set_origin(repo, None)
    conn = connect(tmp_path / "i.db")
    rid = register_repository(conn, repo, "main")  # shim: local:repo@main
    _add_commit_row(conn, rid)
    _set_origin(repo, "git@github.com:dahep/dotfiles.git")
    with pytest.warns(UserWarning, match="replacing repository identity"):
        assert register_repository(conn, repo, "main") == rid
    row = conn.execute("SELECT repo_key FROM repositories").fetchone()
    assert row["repo_key"] == "https://github.com/dahep/dotfiles@main"
