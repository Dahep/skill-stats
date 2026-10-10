"""Metrics tests and CLI wiring against a fixture repo."""

import json

import pytest

from conftest import add_feature, make_db
from skill_stats.cli import main
from skill_stats.gitwalk import walk
from skill_stats.metrics import snapshot, snapshot_to_json


@pytest.fixture()
def classified(tmp_path, repo, shas):
    conn = make_db(tmp_path / "m.db", repo)
    walk(repo, conn, "main")
    for key, verdict in (
        ("c1", "feature"),
        ("c2", "feature"),
        ("c3", "fix"),
        ("c4", "refactor"),
        ("c5", "fix"),
        ("c6", "feature"),
        ("cr", "feature"),
        ("c7", "feature"),
        ("c8", "cleanup"),
    ):
        conn.execute(
            "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
            " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, '', '', 'm','x')",
            (shas[key], verdict),
        )
    fid = add_feature(conn, "alpha", "x", "2026-01-01")
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (shas["c1"], fid),
    )
    conn.commit()
    yield conn


def test_snapshot_counts(classified):
    snap = snapshot(classified)
    assert snap.total_commits == 9
    assert snap.verdict_counts == {"feature": 5, "fix": 2, "refactor": 1, "cleanup": 1}
    assert snap.total_features == 1
    assert snap.total_fixes == 2


def test_snapshot_json_shape(classified):
    data = json.loads(snapshot_to_json(snapshot(classified)))
    assert data["features"] == 1
    assert data["fixes"] == 2
    assert set(data) >= {"total_commits", "verdicts", "features_per_year", "fixes_per_feature"}


def test_populate_fix_rows_then_metrics(classified, repo, shas):
    from skill_stats.lineage import close_lineage
    from skill_stats.targets import annotate_all

    n = annotate_all(repo, classified)
    assert n == 2
    close_lineage(classified)
    snap = snapshot(classified)
    assert snap.fixes_per_feature == 2.0
    assert snap.top_features_by_fixes[0][2] == 2
    # c3's written a.txt has 1 del + 1 add = 2 churn lines; feature alpha churn from c1's diff
    assert snap.top_features_by_churn[0][2] >= 2


def test_cli_init_walk(tmp_path, repo, shas, capsys, monkeypatch):
    db_path = tmp_path / "cli.db"
    main(["init", str(repo), "--db", str(db_path)])
    main(["walk", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert "walked 9 new commits" in out
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 9
    # init registered THE repository row and stored its id in settings
    row = conn.execute("SELECT * FROM repositories").fetchone()
    assert row["repo_key"] == "local:repo@main"
    rid = conn.execute("SELECT value FROM settings WHERE key = 'repo_id'").fetchone()[0]
    assert json.loads(rid) == row["id"]
    assert conn.execute("SELECT COUNT(DISTINCT repo_id) FROM commits").fetchone()[0] == 1


def test_cli_report_text(classified, capsys):
    with pytest.MonkeyPatch.context() as mp:
        import skill_stats.report as report

        mp.setattr(report, "snapshot", lambda conn, top=15: snapshot(conn, top))
        main(["report"])
    out = capsys.readouterr().out
    assert "Features / year" in out


def test_rolling_and_timeline(classified, repo):
    from skill_stats.lineage import close_lineage
    from skill_stats.metrics import features_timeline, fixes_rolling
    from skill_stats.targets import annotate_all

    assert annotate_all(repo, classified) == 2
    close_lineage(classified)
    tl = features_timeline(classified)
    assert tl and tl[-1].features_cumulative == 1
    assert sum(p.features_new for p in tl) == 1

    roll = fixes_rolling(classified)
    assert roll and all(p.fixes_per_feature >= 0 for p in roll)
    # c3 (targets c1 defining alpha) + c5 (fix-of-fix, lineage -> alpha) both
    # land on the same day: one sample, 2 fixes, 1 distinct feature touched
    assert roll[-1].fixes_window == 2
    assert roll[-1].features_touched == 1
    assert roll[-1].fixes_per_feature == 2.0


def test_rolling_and_timeline_empty_db(tmp_path):
    from skill_stats.db import connect
    from skill_stats.metrics import features_timeline, fixes_rolling

    conn = connect(tmp_path / "empty.db")
    assert features_timeline(conn) == []
    assert fixes_rolling(conn) == []


def test_html_report(tmp_path, classified, repo):
    from skill_stats.htmlreport import write_report
    from skill_stats.lineage import close_lineage
    from skill_stats.metrics import feature_ranking
    from skill_stats.targets import annotate_all

    assert annotate_all(repo, classified) == 2
    close_lineage(classified)

    out = write_report(classified, tmp_path / "r.html")
    text = out.read_text(encoding="utf-8")
    assert "<svg" in text and "skill-stats report" in text
    assert "alpha" in text  # fixture feature named in ranking table
    ranking = feature_ranking(classified)[:5]
    assert ranking and ranking[0].fid == "repo-1" and ranking[0].fixes == 2
