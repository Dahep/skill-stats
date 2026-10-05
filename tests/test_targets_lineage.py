"""Blame evidence + fix target reduction + lineage closure tests."""

import pytest

from skill_stats.blame import blame_deleted_lines, contiguous, delete_ranges
from skill_stats.db import connect
from skill_stats.gitwalk import walk
from skill_stats.lineage import close_lineage
from skill_stats.targets import annotate_all, annotate_fix


@pytest.fixture()
def db(repo, shas):
    conn = connect(repo / "t.db")
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
        " JOIN commits f ON f.id = t.fix_commit_id WHERE f.sha = ?", (shas["c3"],)
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
    db.execute(
        "INSERT INTO features (title, about, created_at) VALUES ('alpha module', 'x',"
        " '2026-01-01T00:00:00Z')"
    )
    feat_a = db.execute("SELECT id FROM features WHERE title = 'alpha module'").fetchone()[0]
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
    db.execute("INSERT INTO features (title, about, created_at) VALUES ('alpha', 'x', '2026')")
    feat_a = db.execute("SELECT id FROM features WHERE title = 'alpha'").fetchone()[0]
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
    conn = connect(repo / "x.db")
    walk(repo, conn, "main")
    _verdict_fix(conn, shas["c3"])
    conn.commit()
    annotate_all(repo, conn)
    assert close_lineage(conn) == 0
