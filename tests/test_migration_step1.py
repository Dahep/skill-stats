"""Migration step 0 -> 1 tests: repo-scoped ids, feature-id prefixing, churn columns.

The step-0 fixture DB is built by hand (step 0 SQL + old-shape rows) and then
opened through db.connect, which applies step 1. Never touches any live DB.
"""

import json
import sqlite3

import pytest

from skill_stats.db import connect
from skill_stats.schema import SCHEMA_STEPS

DIFF_C1 = "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+alpha\n+beta\n+charlie\n"
# mixed: normal file change plus an artifact-store section
DIFF_C2 = (
    "diff --git a/n.txt b/n.txt\n"
    "--- a/n.txt\n"
    "+++ b/n.txt\n"
    "@@ -1 +1 @@\n"
    "-one\n"
    "+ONE\n"
    "diff --git a/.skill-stats/artifact.sql b/.skill-stats/artifact.sql\n"
    "--- a/.skill-stats/artifact.sql\n"
    "+++ b/.skill-stats/artifact.sql\n"
    "@@ -0,0 +1,2 @@\n"
    "+INSERT INTO x VALUES (1);\n"
    "+INSERT INTO x VALUES (2);\n"
)
# store-only: stripped diff is empty, churn is zero
DIFF_C3 = (
    "diff --git a/.skill-stats/report.html b/.skill-stats/report.html\n"
    "--- a/.skill-stats/report.html\n"
    "+++ b/.skill-stats/report.html\n"
    "@@ -0,0 +1 @@\n"
    "+<html></html>\n"
)


def build_step0_db(path, with_settings=True, with_rows=True):
    conn = sqlite3.connect(path)
    step0 = SCHEMA_STEPS[0]
    assert isinstance(step0, str)
    conn.executescript(step0)
    conn.execute("CREATE TABLE _schema_step_0000 (ok)")
    if with_settings:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('repo', ?)",
            (json.dumps("/home/tester/dotfiles"),),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('branch', ?)", (json.dumps("main"),)
        )
    if with_rows:
        commits = [
            (
                1,
                "a" * 40,
                None,
                "t1",
                "2026-01-01T00:00:00Z",
                "Tester",
                "feature one",
                "m1",
                "p1",
                1,
                "normal",
                DIFF_C1,
            ),
            (
                2,
                "b" * 40,
                "a" * 40,
                "t2",
                "2026-01-02T00:00:00Z",
                "Tester",
                "fix one",
                "m2",
                "p2",
                2,
                "normal",
                DIFF_C2,
            ),
            (
                3,
                "c" * 40,
                "b" * 40,
                "t3",
                "2026-01-03T00:00:00Z",
                "Tester",
                "artifact",
                "m3",
                "p3",
                3,
                "normal",
                DIFF_C3,
            ),
        ]
        conn.executemany(
            """INSERT INTO commits (id, sha, parent_sha, tree_sha, committed_at, author_name,
               title, message, subject_patch_id, walk_index, kind, diff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            commits,
        )
        conn.executemany(
            "INSERT INTO features (id, title, about, created_at) VALUES (?, ?, ?, ?)",
            [
                (3, "alpha module", "first", "2026-01-01T00:00:00Z"),
                (7, "beta index", "second", "2026-01-02T00:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, ?)",
            [(1, 3, "defines"), (2, 7, "defines")],
        )
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES (2, 'fix', 'r', '', 'm', '2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (2, 1, 1)"
        )
        conn.execute("INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (2, 1)")
        conn.execute(
            "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
            " VALUES (2, 3, NULL)"
        )
    conn.commit()
    conn.close()


def migrated(tmp_path):
    path = tmp_path / "step0.db"
    build_step0_db(path)
    return connect(path)


def test_migration_creates_shim_repository_row(tmp_path):
    conn = migrated(tmp_path)
    row = conn.execute("SELECT * FROM repositories").fetchone()
    # machine path is never identity: documented local: shim, key = base@branch
    assert row["repo_key"] == "local:dotfiles@main"
    assert row["slug"] == "dotfiles"
    assert row["branch"] == "main"
    assert row["clone_url"] is None
    rid = conn.execute("SELECT value FROM settings WHERE key = 'repo_id'").fetchone()[0]
    assert json.loads(rid) == row["id"]
    conn.close()


def test_migration_prefixes_feature_ids(tmp_path):
    conn = migrated(tmp_path)
    rows = conn.execute("SELECT id, title, about, created_at FROM features ORDER BY id").fetchall()
    assert [(r["id"], r["title"], r["about"]) for r in rows] == [
        ("dotfiles-3", "alpha module", "first"),
        ("dotfiles-7", "beta index", "second"),
    ]
    assert rows[0]["created_at"] == "2026-01-01T00:00:00Z"
    links = conn.execute(
        "SELECT commit_id, feature_id, role FROM commits_features ORDER BY commit_id"
    ).fetchall()
    assert [(r["commit_id"], r["feature_id"], r["role"]) for r in links] == [
        (1, "dotfiles-3", "defines"),
        (2, "dotfiles-7", "defines"),
    ]
    ff = conn.execute(
        "SELECT fix_commit_id, feature_id, via_fix_commit_id FROM fixes_features"
    ).fetchall()
    assert [(r["fix_commit_id"], r["feature_id"], r["via_fix_commit_id"]) for r in ff] == [
        (2, "dotfiles-3", None)
    ]
    conn.close()


def test_migration_commits_repo_scoped_with_churn(tmp_path):
    conn = migrated(tmp_path)
    rid = conn.execute("SELECT id FROM repositories").fetchone()["id"]
    rows = conn.execute("SELECT * FROM commits ORDER BY walk_index").fetchall()
    assert [r["id"] for r in rows] == [1, 2, 3]  # ids stable, insertion order preserved
    assert [r["walk_index"] for r in rows] == [1, 2, 3]
    assert all(r["repo_id"] == rid for r in rows)
    # churn derived from stored diffs with the _diff_churn counting rules
    assert (rows[0]["added_lines"], rows[0]["deleted_lines"], rows[0]["churn_lines"]) == (3, 0, 3)
    # the store section does not count: only -one/+ONE
    assert (rows[1]["added_lines"], rows[1]["deleted_lines"], rows[1]["churn_lines"]) == (1, 1, 2)
    assert (rows[2]["added_lines"], rows[2]["deleted_lines"], rows[2]["churn_lines"]) == (0, 0, 0)
    conn.close()


def test_migration_strips_store_paths_from_stored_diffs(tmp_path):
    conn = migrated(tmp_path)
    diffs = {r["sha"]: r["diff"] for r in conn.execute("SELECT sha, diff FROM commits")}
    assert ".skill-stats" not in diffs["b" * 40]
    assert "n.txt" in diffs["b" * 40]
    assert diffs["c" * 40] == ""  # store-only diff becomes empty
    conn.close()


def test_migration_commit_uniques_are_repo_scoped(tmp_path):
    conn = migrated(tmp_path)
    rid = conn.execute("SELECT id FROM repositories").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO commits (repo_id, sha, tree_sha, committed_at, author_name, title,"
            " message, walk_index, kind, diff) VALUES (?, ?, 't', 'd', 'a', 't', 'm', 99,"
            " 'normal', '')",
            (rid, "a" * 40),
        )
    # same sha under a different repo is fine
    cur = conn.execute(
        "INSERT INTO repositories (repo_key, slug, branch, clone_url, added_at)"
        " VALUES ('other@main', 'other', 'main', NULL, '2026')"
    )
    other = cur.lastrowid
    conn.execute(
        "INSERT INTO commits (repo_id, sha, tree_sha, committed_at, author_name, title,"
        " message, walk_index, kind, diff) VALUES (?, ?, 't', 'd', 'a', 't', 'm', 1,"
        " 'normal', '')",
        (other, "a" * 40),
    )
    with pytest.raises(sqlite3.IntegrityError):  # walk_index unique per repo
        conn.execute(
            "INSERT INTO commits (repo_id, sha, tree_sha, committed_at, author_name, title,"
            " message, walk_index, kind, diff) VALUES (?, 'd' * 40, 't', 'd', 'a', 't', 'm', 1,"
            " 'normal', '')",
            (other,),
        )
    conn.close()


def test_migration_preserves_attribution_tables(tmp_path):
    conn = migrated(tmp_path)
    vt = conn.execute("SELECT commit_id, verdict FROM commit_verdicts").fetchall()
    assert [(r["commit_id"], r["verdict"]) for r in vt] == [(2, "fix")]
    ft = conn.execute(
        "SELECT fix_commit_id, source_commit_id, hit_lines FROM fix_touches"
    ).fetchall()
    assert [(r["fix_commit_id"], r["source_commit_id"], r["hit_lines"]) for r in ft] == [(2, 1, 1)]
    tg = conn.execute("SELECT fix_commit_id, target_commit_id FROM fix_targets").fetchall()
    assert [(r["fix_commit_id"], r["target_commit_id"]) for r in tg] == [(2, 1)]
    conn.close()


def test_migration_step_is_bookkept_and_rerun_safe(tmp_path):
    path = tmp_path / "step0.db"
    build_step0_db(path)
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "_schema_step_0001" in tables
    conn.close()
    conn2 = connect(path)  # second open must not re-run step 1
    assert conn2.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 1
    conn2.close()


def test_migration_fails_loudly_without_identity(tmp_path):
    path = tmp_path / "no-settings.db"
    build_step0_db(path, with_settings=False)
    with pytest.raises(RuntimeError, match="settings.repo"):
        connect(path)


def test_fresh_db_ends_at_step_1_shape(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(commits)")}
    assert {"repo_id", "added_lines", "deleted_lines", "churn_lines"} <= cols
    ftype = conn.execute("PRAGMA table_info(features)").fetchall()
    assert any(r["name"] == "id" and r["type"] == "TEXT" for r in ftype)
    assert conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 0
    conn.close()
