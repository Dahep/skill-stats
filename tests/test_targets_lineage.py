"""Blame evidence + fix target reduction + lineage closure tests."""

import subprocess

import pytest

from conftest import add_feature, commit_files, make_db, run
from skill_stats.blame import blame_deleted_lines, contiguous, delete_ranges
from skill_stats.gitwalk import walk
from skill_stats.lineage import close_lineage
from skill_stats.targets import annotate_all, annotate_fix


@pytest.fixture()
def db(repo, shas):
    conn = make_db(repo / "t.db", repo)
    walk(repo, conn, "main")
    return conn


def test_delete_ranges(repo, shas):
    ranges = delete_ranges(repo, shas["c3"])
    assert ranges == {"a.txt": [(1, 1)]}
    # reorder commit: git's minimal edit script is "delete charlie (line3), insert at top"
    assert delete_ranges(repo, shas["c4"]) == {"a.txt": [(3, 1)]}
    # blank-line-only commit deletes nothing -> no targets later
    assert delete_ranges(repo, shas["c8"]) == {}


def test_contiguous_merges_adjacent():
    assert contiguous([(3, 2), (5, 1), (10, 1), (11, 2)]) == [(3, 3), (10, 3)]


def test_blame_deleted_lines(repo, shas):
    lines = blame_deleted_lines(repo, shas["c3"])
    assert lines == {"a.txt": [shas["c1"]]}

    # c5 rewrites ALPHA (introduced by c3) while parent is c4
    lines5 = blame_deleted_lines(repo, shas["c5"])
    assert set(lines5["a.txt"]) == {shas["c3"]}

    # patch-id-duplicate content (c7 introducing dup.txt) has no deletions
    assert blame_deleted_lines(repo, shas["c7"]) == {}


def test_annotate_fix_then_annotate_all(repo, shas, db):
    for key in ("c3", "c5"):
        db.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), 'fix', 'x', '',"
            " 'm', '2026-01-01T00:00:00Z')",
            (shas[key],),
        )
    db.commit()

    annotate_fix(repo, db, shas["c3"], 1)
    touches = db.execute(
        "SELECT c.sha, t.hit_lines FROM fix_touches t"
        " JOIN commits c ON c.id = t.source_commit_id"
        " JOIN commits f ON f.id = t.fix_commit_id WHERE f.sha = ?",
        (shas["c3"],),
    ).fetchall()
    assert [(r["sha"], r["hit_lines"]) for r in touches] == [(shas["c1"], 1)]

    n = annotate_all(repo, db)
    assert n == 2
    targets = {
        (f_sha, t_sha)
        for f_sha, t_sha in db.execute(
            """SELECT f.sha, t.sha FROM fix_targets ft
               JOIN commits f ON f.id = ft.fix_commit_id
               JOIN commits t ON t.id = ft.target_commit_id"""
        )
    }
    assert targets == {(shas["c3"], shas["c1"]), (shas["c5"], shas["c3"])}


def test_refactor_and_whitespace_get_no_fix_rows(repo, shas, db):
    for key, verdict in (("c4", "refactor"), ("c8", "cleanup")):
        db.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, '', '', 'm','x')",
            (shas[key], verdict),
        )
    db.commit()
    assert annotate_all(repo, db) == 0
    assert db.execute("SELECT COUNT(*) c FROM fix_touches").fetchone()["c"] == 0


def _verdict_fix(db, sha):
    db.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), 'fix', '', '', 'm','x')",
        (sha,),
    )


def test_lineage_transitive_and_via(repo, shas, db):
    _verdict_fix(db, shas["c3"])
    _verdict_fix(db, shas["c5"])
    feat_a = add_feature(db, "alpha module", "x", "2026-01-01T00:00:00Z")
    db.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (shas["c1"], feat_a),
    )
    db.commit()

    annotate_all(repo, db)
    added = close_lineage(db)
    assert added == 2  # c3->F, c5->F

    rows = {
        (r["fix"], r["feat"], r["via"])
        for r in db.execute(
            """SELECT f.sha fix, ft.title feat, vf.sha via FROM fixes_features ff
               JOIN commits f ON f.id = ff.fix_commit_id
               JOIN features ft ON ft.id = ff.feature_id
               LEFT JOIN commits vf ON vf.id = ff.via_fix_commit_id"""
        )
    }
    assert rows == {(shas["c3"], "alpha module", None), (shas["c5"], "alpha module", shas["c3"])}


def test_lineage_idempotent(repo, shas, db):
    _verdict_fix(db, shas["c3"])
    _verdict_fix(db, shas["c5"])
    feat_a = add_feature(db, "alpha", "x", "2026")
    db.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (shas["c1"], feat_a),
    )
    db.commit()
    annotate_all(repo, db)
    close_lineage(db)
    n_first = db.execute("SELECT COUNT(*) c FROM fixes_features").fetchone()["c"]
    close_lineage(db)
    assert db.execute("SELECT COUNT(*) c FROM fixes_features").fetchone()["c"] == n_first


def test_lineage_requires_defines(tmp_path, repo, shas):
    # no features defined at all -> no closure rows
    conn = make_db(repo / "x.db", repo)
    walk(repo, conn, "main")
    _verdict_fix(conn, shas["c3"])
    conn.commit()
    annotate_all(repo, conn)
    assert close_lineage(conn) == 0


def test_blame_ignores_store_paths(repo):
    c1 = commit_files(repo, "content", {"n.txt": "one\ntwo\n", ".skill-stats/a.sql": "old\n"})
    (repo / "n.txt").write_text("ONE\ntwo\n")
    (repo / ".skill-stats" / "a.sql").unlink()
    run(repo, "add", "-A")
    run(repo, "commit", "-m", "drop store, rewrite content")
    c2 = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    # the store-path deletion never contributes blame evidence
    lines = blame_deleted_lines(repo, c2)
    assert set(lines) == {"n.txt"}
    assert lines["n.txt"] == [c1]


def test_blame_rename_into_store_contributes_no_fix_attribution(repo, shas=None):
    c1 = commit_files(repo, "content", {"lib.ts": "one\ntwo\nthree\n", "data.txt": "a\nb\n"})
    # one commit: a modified rename of lib.ts INTO the store + a genuine fix
    (repo / ".skill-stats").mkdir()
    run(repo, "mv", "lib.ts", ".skill-stats/lib.ts")
    (repo / ".skill-stats" / "lib.ts").write_text("one\nTWO\nthree\n")
    (repo / "data.txt").write_text("a\n")  # genuine product fix: drop line b
    run(repo, "add", "-A")
    run(repo, "commit", "-m", "fix: drop b, park lib in store")
    c2 = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    # blame-level: the renamed (artifact-bound) hunks never appear
    lines = blame_deleted_lines(repo, c2)
    assert set(lines) == {"data.txt"}
    assert lines["data.txt"] == [c1]

    # attribution-level: exactly one fix touch, from the genuine fix only
    conn = make_db(repo / "r.db", repo)
    walk(repo, conn, "main")
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), 'fix', '', '', 'm',"
        " '2026-01-01T00:00:00Z')",
        (c2,),
    )
    conn.commit()
    annotate_fix(repo, conn, c2, 1)
    touches = conn.execute(
        "SELECT t.hit_lines FROM fix_touches t"
        " JOIN commits f ON f.id = t.fix_commit_id WHERE f.sha = ?",
        (c2,),
    ).fetchall()
    assert [r["hit_lines"] for r in touches] == [1]
    conn.close()
