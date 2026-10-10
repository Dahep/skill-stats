"""Live-lines tests: tip attribution, fix-through-lineage accrual, renames,
unattributed counting, Sample backfill (full sweep + capped plan)."""

import json
import subprocess

import pytest

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
    _verdict(conn, h1, "feature")  # classified: the sweep may complete
    conn.commit()
    update_live_lines(conn, repo)
    before = set(conn.execute("SELECT * FROM feature_line_samples"))
    res2 = update_live_lines(conn, repo)
    assert res2.backfilled_points == 0  # samples already complete -> no re-sweep
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


# ------------------------------------- quoted names + first-parent blaming --


def test_live_lines_handles_non_ascii_names_and_quoted_store_paths(repo, tmp_path):
    # git quotes non-ASCII names in text ls-tree output: the live-lines file
    # listing must use raw names, and store-path exclusion must see decoded
    # names even when the store file itself is non-ASCII
    h1 = commit_files(repo, "feature a", {"café.txt": "a1\na2\n"})
    commit_files(repo, "store only", {".skill-stats/ünïcode.sql": "INSERT\n"})
    h3 = commit_files(repo, "tip work", {"k.txt": "k1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")  # the store-only commit is skipped by the walk
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.at_sha == h3
    # café.txt's lines attribute despite the quoted spelling; the non-ASCII
    # store file is excluded on its decoded name (not blamed at all)
    assert res.unattributed_lines == 0
    assert dict(conn.execute("SELECT id, live_lines FROM features")) == {fa: 2}


def test_merge_brought_lines_attribute_to_the_merge_commit(repo, tmp_path):
    commit_files(repo, "base", {"b.txt": "b\n"})
    run(repo, "checkout", "-b", "side")
    commit_files(repo, "side feature", {"s.txt": "s1\ns2\n"})
    run(repo, "checkout", "main")
    run(repo, "merge", "--no-ff", "side", "-m", "merge side feature")
    merge = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")  # first-parent walk: base + merge; side is absent
    fm = add_feature(conn, "side feature")
    _define(conn, fm, merge)
    conn.commit()
    res = update_live_lines(conn, repo)
    # blame --first-parent assigns the side branch's lines to the merge commit
    # (which the walk includes) -> its features; nothing goes unattributed
    assert res.unattributed_lines == 0
    assert dict(conn.execute("SELECT id, live_lines FROM features")) == {fm: 2}


# ------------------------------------------- backfill trust + report wording --


def _state(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'backfill_state'").fetchone()
    return json.loads(row[0]) if row else None


def _verdicted_feature_repo(repo, tmp_path):
    """h1/h2 both classified (no backlog), each defining one feature."""
    h1 = commit_files(repo, "first", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "second", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    fb = add_feature(conn, "feature b")
    _define(conn, fa, h1)
    _define(conn, fb, h2)
    _verdict(conn, h1, "feature")
    _verdict(conn, h2, "feature")
    conn.commit()
    return conn, fa, fb, h1, h2


def test_backfill_resumes_after_interruption(repo, tmp_path, monkeypatch):
    conn, fa, fb, h1, h2 = _verdicted_feature_repo(repo, tmp_path)
    import skill_stats.livelines as livelines

    real = livelines._line_counts_at
    calls = {"n": 0, "boom": True}

    def flaky(repo_dir, sha):
        calls["n"] += 1
        if calls["boom"] and calls["n"] == 2:
            raise RuntimeError("injected sweep failure")
        return real(repo_dir, sha)

    monkeypatch.setattr(livelines, "_line_counts_at", flaky)
    with pytest.raises(RuntimeError, match="injected"):
        update_live_lines(conn, repo)
    assert _state(conn) is None  # interrupted sweep records no completeness

    calls["boom"] = False
    res = update_live_lines(conn, repo)  # a normal update resumes the sweep
    assert res.backfilled_points == 2
    assert _state(conn) == {"status": "done", "max_classified_walk_index": 2}
    points = {r[0] for r in conn.execute("SELECT at_commit_sha FROM feature_line_samples")}
    assert points == {h1, h2}
    conn.close()


def test_backfill_resweeps_for_late_classification(repo, tmp_path):
    h1 = commit_files(repo, "first", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "second", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    # classify only h2 first: h1 stays pending -> the sweep cannot complete
    fb = add_feature(conn, "feature b")
    _define(conn, fb, h2)
    _verdict(conn, h2, "feature")
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.backfilled_points == 2
    assert _state(conn)["status"] == "pending"  # backlog keeps trust pending

    # classify h1 late: the feature defined at the old commit must gain its
    # historical sample points via a re-sweep
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    _verdict(conn, h1, "feature")
    conn.commit()
    res2 = update_live_lines(conn, repo)
    assert res2.backfilled_points == 2  # re-swept
    rows = {
        (r["feature_id"], r["at_commit_sha"])
        for r in conn.execute("SELECT feature_id, at_commit_sha FROM feature_line_samples")
    }
    assert (fa, h1) in rows and (fa, h2) in rows
    assert _state(conn) == {"status": "done", "max_classified_walk_index": 2}
    conn.close()


def test_backfill_done_state_is_stable(repo, tmp_path):
    conn, fa, fb, h1, h2 = _verdicted_feature_repo(repo, tmp_path)
    update_live_lines(conn, repo)
    assert _state(conn)["status"] == "done"
    before = set(conn.execute("SELECT * FROM feature_line_samples"))
    res = update_live_lines(conn, repo)  # no new classification -> no re-sweep
    assert res.backfilled_points == 0
    assert set(conn.execute("SELECT * FROM feature_line_samples")) == before
    conn.close()


def test_known_commit_without_links_is_neither_feature_nor_unattributed(repo, tmp_path):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "cleanup", {"k.txt": "k1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    _define(conn, fa, h1)
    _verdict(conn, h1, "feature")
    _verdict(conn, h2, "cleanup")  # known + classified, but claims no feature
    conn.commit()
    res = update_live_lines(conn, repo)
    assert res.unattributed_lines == 0  # unattributed = introducing commit UNKNOWN
    # k.txt's line accrues to no feature — it is neither counted nor dropped
    # from the tip: it simply belongs to no feature
    assert dict(conn.execute("SELECT id, live_lines FROM features")) == {fa: 2}
    conn.close()


def test_report_marks_incomplete_sampling_and_renames_total(repo, tmp_path):
    from skill_stats import htmlreport

    h1 = commit_files(repo, "first", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "second", {"g.txt": "b1\n"})
    conn = make_db(tmp_path / "l.db", repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    fb = add_feature(conn, "feature b")
    _define(conn, fa, h1)
    _define(conn, fb, h2)
    conn.commit()
    update_live_lines(conn, repo)  # no verdicts -> backlog -> state pending
    page = htmlreport.build(conn)
    assert "total attributed lines" in page  # renamed total
    assert "can exceed" in page  # multi-feature claims can exceed repo size
    assert "sampling incomplete" in page.lower()
    assert "unknown, not zero" in page

    _verdict(conn, h1, "feature")
    _verdict(conn, h2, "feature")
    conn.commit()
    update_live_lines(conn, repo)  # backlog cleared -> sweep completes
    page2 = htmlreport.build(conn)
    assert "sampling incomplete" not in page2.lower()
    conn.close()


def test_chart_draws_gaps_not_zeros_when_incomplete():
    from skill_stats import htmlreport

    chart = htmlreport._lines_chart(
        ["a", "b", "c"], [("s", [1.0, None, 3.0], "#123456")], ("t", [1.0, 1.0, 3.0], "#654321")
    )
    # the gapped series renders as two segments (values only where samples
    # exist), never as a zero-filled line
    assert chart.count('stroke="#123456"') == 2
    assert chart.count('stroke="#654321"') == 1
