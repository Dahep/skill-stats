"""Live-lines tests: tip attribution, fix-through-lineage accrual, renames,
unattributed counting, Sample backfill (full sweep + capped plan)."""

import subprocess

from conftest import add_feature, commit_files, make_db, run
from skill_stats.blame import _porcelain_shas
from skill_stats.gitwalk import walk
from skill_stats.lineage import close_lineage
from skill_stats.livelines import sample_points, update_live_lines
from skill_stats.targets import annotate_all


def _verdict(conn, sha, verdict):
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, '', '', 'm',"
        " '2026-01-01T00:00:00Z')",
        (sha, verdict),
    )


def _define(conn, fid, sha):
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (sha, fid),
    )


def test_live_lines_defines_only_and_fix_through_lineage(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "fix a1", {"f.txt": "A1\na2\n"})
    h3 = commit_files(repo, "feature b", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    fb = add_feature(conn, "feature b")
    _define(conn, fa, h1)
    _define(conn, fb, h3)
    for sha, v in ((h1, "feature"), (h2, "fix"), (h3, "feature")):
        _verdict(conn, sha, v)
    conn.commit()
    annotate_all(repo, conn)  # fix_touches/fix_targets h2 -> h1
    close_lineage(conn)  # fixes_features h2 -> fa

    res = update_live_lines(conn, repo)
    assert res.at_sha == h3
    assert res.unattributed_lines == 0
    live = dict(conn.execute("SELECT id, live_lines FROM features"))
    # a2 via defines(h1) + A1 via the fix's lineage closure -> fa; b1 -> fb
    assert live == {fa: 2, fb: 1}
    samples = {
        (r["feature_id"], r["at_commit_sha"], r["live_lines"])
        for r in conn.execute("SELECT * FROM feature_line_samples")
    }
    assert samples == {
        (fa, h1, 2),  # both f.txt lines introduced by h1
        (fa, h2, 2),  # A1 accrues through the closure, a2 through defines
        (fa, h3, 2),
        (fb, h3, 1),  # fb does not exist yet at h1/h2 -> no rows there
    }
    conn.close()


def test_live_lines_follow_renames(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    run(repo, "mv", "f.txt", "renamed.txt")
    run(repo, "commit", "-m", "move into place")
    h2 = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.at_sha == h2
    # blame follows the rename: both lines still owned by h1
    assert dict(conn.execute("SELECT id, live_lines FROM features")) == {fa: 2}


def test_unattributed_lines_are_counted_not_dropped(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "loose work", {"g.txt": "b1\n"})
    h3 = commit_files(repo, "tip work", {"k.txt": "k1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    # h2 exists in git but not in the DB (outside the walk): its line at the
    # tip is unattributable and must surface as a count
    conn.execute("DELETE FROM commits WHERE sha = ?", (h2,))
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.at_sha == h3  # tip = latest walked commit
    assert res.unattributed_lines == 1  # g.txt's single line
    assert dict(conn.execute("SELECT id, live_lines FROM features")) == {fa: 2}


def test_backfill_samples_every_walked_commit(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "feature b", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    fb = add_feature(conn, "feature b")
    _define(conn, fa, h1)
    _define(conn, fb, h2)
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.backfilled_points == 2  # full sweep: one point per walked commit
    points = {
        r["at_commit_sha"] for r in conn.execute("SELECT at_commit_sha FROM feature_line_samples")
    }
    assert points == {h1, h2}
    # fa alive at h1 with 2 lines; fb not alive at h1
    rows = {
        (r["feature_id"], r["at_commit_sha"], r["live_lines"])
        for r in conn.execute("SELECT * FROM feature_line_samples")
    }
    assert rows == {(fa, h1, 2), (fa, h2, 2), (fb, h2, 1)}


def test_update_live_lines_is_idempotent(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    conn.commit()
    update_live_lines(conn, repo)
    before = set(conn.execute("SELECT * FROM feature_line_samples"))
    res2 = update_live_lines(conn, repo)
    assert res2.backfilled_points == 0  # samples already exist -> no re-sweep
    assert set(conn.execute("SELECT * FROM feature_line_samples")) == before


def test_sample_points_full_then_capped():
    # full sweep under the cap
    assert sample_points(list(range(1, 101))) == list(range(1, 101))
    assert sample_points(list(range(1, 401))) == list(range(1, 401))
    # past the cap: evenly spaced, ends included, no duplicates
    pts = sample_points(list(range(1, 501)))
    assert pts[0] == 1 and pts[-1] == 500
    assert 150 <= len(pts) <= 160
    assert pts == sorted(set(pts))
    assert sample_points(list(range(1, 501))) == pts  # deterministic


def test_porcelain_parser_on_real_blame_output(repo, shas):
    out = subprocess.run(
        ["git", "-C", str(repo), "blame", "-w", "-C", "--porcelain", shas["c8"], "--", "a.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    blamed = _porcelain_shas(out)
    assert len(blamed) == 4  # a.txt has 4 lines at the tip
    assert set(blamed) <= set(shas.values())  # every line maps to a walked commit


# ------------------------------------------------------- report additions --


def test_report_renders_live_lines_column_and_size_chart(repo, tmp_path):
    from skill_stats import htmlreport

    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "feature b", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    fb = add_feature(conn, "feature b")
    _define(conn, fa, h1)
    _define(conn, fb, h2)
    conn.commit()
    update_live_lines(conn, repo)
    page = htmlreport.build(conn)
    assert "Live lines" in page  # ranking table column
    assert "Feature size over time" in page  # sample-driven chart section
    assert "feature a" in page and "feature b" in page
    assert "unattributed" in page  # data-quality note (0 here)


def test_report_graceful_without_samples(repo, tmp_path):
    from skill_stats import htmlreport

    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    conn.commit()
    page = htmlreport.build(conn)  # live-lines never ran: no samples
    assert "Live lines" in page
    assert "Feature size over time" not in page  # section omitted, page still renders
    assert "skill-stats report" in page
