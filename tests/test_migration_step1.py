"""Migration step 0 -> 1 tests: repo-scoped ids, feature-id prefixing, churn columns,
stale-commit purge, atomic rollback, and moved==fresh metric equivalence.

The step-0 fixture DBs are built by hand (step 0 SQL + old-shape rows) and then
opened through db.connect, which applies step 1. Never touches any live DB.
"""

import json
import sqlite3

import pytest

import skill_stats.schema as schema
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
# store-only: stripped diff is empty; the commit never enters stats
DIFF_C3 = (
    "diff --git a/.skill-stats/report.html b/.skill-stats/report.html\n"
    "--- a/.skill-stats/report.html\n"
    "+++ b/.skill-stats/report.html\n"
    "@@ -0,0 +1 @@\n"
    "+<html></html>\n"
)


def build_step0_db(path, repo_path="/home/tester/dotfiles", with_settings=True, with_rows=True):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    step0 = SCHEMA_STEPS[0]
    assert isinstance(step0, str)
    conn.executescript(step0)
    conn.execute("CREATE TABLE _schema_step_0000 (ok)")
    if with_settings:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('repo', ?)", (json.dumps(repo_path),)
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
                (9, "artifact junk", "store-only", "2026-01-03T00:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, ?)",
            [(1, 3, "defines"), (2, 7, "defines"), (3, 9, "defines")],
        )
        conn.executemany(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES (?, ?, 'r', '', 'm', '2026-01-02T00:00:00Z')",
            [(2, "fix"), (3, "cleanup")],
        )
        conn.execute(
            "INSERT INTO fix_touches (fix_commit_id, source_commit_id, hit_lines) VALUES (2, 1, 1)"
        )
        conn.execute("INSERT INTO fix_targets (fix_commit_id, target_commit_id) VALUES (2, 1)")
        conn.executemany(
            "INSERT INTO fixes_features (fix_commit_id, feature_id, via_fix_commit_id)"
            " VALUES (?, ?, NULL)",
            [(2, 3), (2, 9)],
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
    # feature 9 (defined only by the store-only commit) is purged as an orphan
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
    # (2, dotfiles-9) died with its orphaned feature
    assert [(r["fix_commit_id"], r["feature_id"], r["via_fix_commit_id"]) for r in ff] == [
        (2, "dotfiles-3", None)
    ]
    conn.close()


def test_migration_commits_repo_scoped_with_churn(tmp_path):
    conn = migrated(tmp_path)
    rid = conn.execute("SELECT id FROM repositories").fetchone()["id"]
    rows = conn.execute("SELECT * FROM commits ORDER BY walk_index").fetchall()
    # ids stable, insertion order preserved; store-only commit 3 is purged
    assert [r["id"] for r in rows] == [1, 2]
    assert [r["walk_index"] for r in rows] == [1, 2]
    assert all(r["repo_id"] == rid for r in rows)
    # churn derived from stored diffs with the _diff_churn counting rules
    assert (rows[0]["added_lines"], rows[0]["deleted_lines"], rows[0]["churn_lines"]) == (3, 0, 3)
    # the store section does not count: only -one/+ONE
    assert (rows[1]["added_lines"], rows[1]["deleted_lines"], rows[1]["churn_lines"]) == (1, 1, 2)
    conn.close()


def test_migration_strips_store_paths_from_stored_diffs(tmp_path):
    conn = migrated(tmp_path)
    diffs = {r["sha"]: r["diff"] for r in conn.execute("SELECT sha, diff FROM commits")}
    assert ".skill-stats" not in diffs["b" * 40]
    assert "n.txt" in diffs["b" * 40]
    assert "c" * 40 not in diffs  # store-only commit purged entirely
    conn.close()


def test_migration_purges_store_only_dependents_and_orphans(tmp_path):
    conn = migrated(tmp_path)
    # no rows anywhere reference the purged store-only commit or its orphan feature
    assert conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 2
    vt = conn.execute("SELECT commit_id, verdict FROM commit_verdicts").fetchall()
    assert [(r["commit_id"], r["verdict"]) for r in vt] == [(2, "fix")]  # cleanup verdict gone
    feats = [r[0] for r in conn.execute("SELECT id FROM features")]
    assert "dotfiles-9" not in feats
    assert conn.execute("SELECT COUNT(*) c FROM commits_features").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM fixes_features").fetchone()["c"] == 1
    # FK integrity across the whole rebuilt schema
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
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


def test_migration_failure_rolls_back_and_reopens(tmp_path, monkeypatch):
    """Atomicity: an injected mid-migration failure rolls EVERYTHING back (DDL
    included) to the step-0 shape, and reopening re-runs the migration."""
    path = tmp_path / "step0.db"
    build_step0_db(path)
    real = schema._rebuild_features
    calls = {"n": 0}

    def boom(conn, repo_id, slug):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected failure")
        return real(conn, repo_id, slug)

    monkeypatch.setattr(schema, "_rebuild_features", boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        connect(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "repositories" not in tables  # the pre-insert CREATE rolled back too
    assert "_schema_step_0001" not in tables
    fcols = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(features)")}
    assert fcols["id"] == "INTEGER"  # old shape intact
    assert conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 3
    conn.close()

    # reopening re-runs step 1 to completion (the hook now delegates)
    conn2 = connect(path)
    assert conn2.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 1
    assert conn2.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 2
    ids = [r[0] for r in conn2.execute("SELECT id FROM features ORDER BY id")]
    assert ids == ["dotfiles-3", "dotfiles-7"]
    conn2.close()


def test_fresh_db_ends_at_step_1_shape(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(commits)")}
    assert {"repo_id", "added_lines", "deleted_lines", "churn_lines"} <= cols
    ftype = conn.execute("PRAGMA table_info(features)").fetchall()
    assert any(r["name"] == "id" and r["type"] == "TEXT" for r in ftype)
    assert conn.execute("SELECT COUNT(*) c FROM repositories").fetchone()["c"] == 0
    conn.close()


# ------------------------------------------------------ moved == fresh -----
# A migrated step-0 DB must carry the same stats as walking the same history
# fresh (spec: store-only commits never enter stats).


CREATED1 = "2026-01-01T00:00:00Z"
CREATED4 = "2026-01-04T00:00:00Z"


def test_migration_matches_fresh_walk(repo, tmp_path):
    from conftest import add_feature, commit_files, make_db
    from skill_stats.gitwalk import author_and_time, diff_for, walk
    from skill_stats.lineage import close_lineage
    from skill_stats.metrics import snapshot, snapshot_to_json
    from skill_stats.targets import annotate_all

    k1 = commit_files(repo, "feature one", {"a.txt": "alpha\nbeta\ncharlie\n"})
    k2 = commit_files(repo, "artifact update", {".skill-stats/artifact.sql": "INSERT\n"})
    k3 = commit_files(repo, "fix alpha", {"a.txt": "ALPHA\nbeta\ncharlie\n"})
    k4 = commit_files(repo, "feature two", {"b.txt": "one\ntwo\n"})
    product_shas = [k1, k3, k4]

    # --- the old (step-0) DB, stored as the pre-wave-1 walk stored it
    old_path = tmp_path / "old.db"
    build_step0_db(old_path, repo_path=str(repo), with_rows=False)
    old = sqlite3.connect(old_path)
    old.row_factory = sqlite3.Row
    hist = [
        (1, k1, None, "feature one"),
        (2, k2, k1, "artifact update"),
        (3, k3, k2, "fix alpha"),
        (4, k4, k3, "feature two"),
    ]
    for idx, sha, parent, title in hist:
        at = author_and_time(repo, sha)
        old.execute(
            """INSERT INTO commits (id, sha, parent_sha, tree_sha, committed_at, author_name,
               title, message, subject_patch_id, walk_index, kind, diff)
               VALUES (?, ?, ?, 't', ?, ?, ?, ?, NULL, ?, 'normal', ?)""",
            (idx, sha, parent, at[1], at[0], title, title, idx, diff_for(repo, sha)),
        )
    old.executemany(
        "INSERT INTO features (id, title, about, created_at) VALUES (?, ?, '', ?)",
        [(1, "alpha module", CREATED1), (7, "junk", CREATED1), (2, "beta index", CREATED4)],
    )
    old.executemany(
        "INSERT INTO commits_features (commit_id, feature_id, role) VALUES (?, ?, 'defines')",
        [(1, 1), (2, 7), (4, 2)],
    )
    old.executemany(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES (?, ?, '', '', 'm', '2026-01-01T00:00:00Z')",
        [(1, "feature"), (2, "cleanup"), (3, "fix"), (4, "feature")],
    )
    old.commit()
    annotate_all(repo, old)  # real blame reduction, as the old pipeline did
    close_lineage(old)
    old.close()

    moved = connect(old_path)

    # --- the same history walked fresh under wave 1
    fresh = make_db(tmp_path / "fresh.db", repo)
    assert walk(repo, fresh, "main") == 3  # store-only commit skipped
    for idx, verdict in ((1, "feature"), (3, "fix"), (4, "feature")):
        fresh.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output,"
            " model, classified_at) VALUES ((SELECT id FROM commits WHERE walk_index = ?), ?,"
            " '', '', 'm', '2026-01-01T00:00:00Z')",
            (idx, verdict),
        )
    add_feature(fresh, "alpha module", "", CREATED1)
    add_feature(fresh, "beta index", "", CREATED4)
    fresh.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE walk_index = 1), 'repo-1', 'defines')"
    )
    fresh.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE walk_index = 4), 'repo-2', 'defines')"
    )
    fresh.commit()
    annotate_all(repo, fresh)
    close_lineage(fresh)

    # --- equivalence: same metrics, same per-commit derived columns
    assert snapshot_to_json(snapshot(moved)) == snapshot_to_json(snapshot(fresh))

    def commit_facts(conn):
        return {
            (
                r["sha"],
                r["walk_index"],
                r["added_lines"],
                r["deleted_lines"],
                r["churn_lines"],
                r["diff"],
            )
            for r in conn.execute(
                "SELECT sha, walk_index, added_lines, deleted_lines, churn_lines, diff FROM commits"
            )
        }

    assert commit_facts(moved) == commit_facts(fresh)
    # store-only sha absent on both sides
    assert {s for s, *_ in commit_facts(moved)} == set(product_shas)

    def fix_pairs(conn):
        return {
            (r["fs"], r["ts"], r["h"])
            for r in conn.execute(
                """SELECT f.sha AS fs, t.sha AS ts, ft.hit_lines AS h FROM fix_touches ft
                   JOIN commits f ON f.id = ft.fix_commit_id
                   JOIN commits t ON t.id = ft.source_commit_id"""
            )
        }

    assert fix_pairs(moved) == fix_pairs(fresh) == {(k3, k1, 1)}
    moved.close()
    fresh.close()
