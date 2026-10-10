"""Walk + reconcile tests."""

import pytest

from conftest import commit_files, make_db
from skill_stats.gitwalk import (
    commit_kind,
    diff_for,
    list_commits,
    patch_id_for,
    prepare_diff,
    to_row,
    walk,
)


def test_walk_imports_in_order(repo, shas):
    db = make_db(repo / "w.db", repo)
    n = walk(repo, db, "main")
    assert n == 9
    rows = db.execute(
        "SELECT sha, walk_index, parent_sha, repo_id FROM commits ORDER BY walk_index"
    ).fetchall()
    assert [r["sha"] for r in rows] == list(shas.values())
    assert rows[0]["parent_sha"] is None
    assert rows[3]["parent_sha"] == shas["c3"]
    rid = db.execute("SELECT id FROM repositories").fetchone()["id"]
    assert all(r["repo_id"] == rid for r in rows)


def test_walk_is_resumable(repo, shas):
    db = make_db(repo / "w.db", repo)
    assert walk(repo, db, "main") == 9
    # appending a commit then walking adds exactly the new one
    (repo / "n.txt").write_text("n\n")
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "new tail"], check=True)
    assert walk(repo, db, "main") == 1
    assert db.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 10


def test_walk_detects_rewritten_history(repo, shas):
    db = make_db(repo / "w.db", repo)
    walk(repo, db, "main")
    db.execute("UPDATE commits SET sha = 'deadbeef' WHERE walk_index = 5")
    db.commit()
    with pytest.raises(RuntimeError, match="walk_index=5"):
        walk(repo, db, "main")


def test_patch_id_dedupes_cherry_picked_content(repo, shas):
    # c6 and c7 (re-added dup.txt) are patch-equivalent through remove-commit in between
    p6 = patch_id_for(repo, shas["c6"])
    p7 = patch_id_for(repo, shas["c7"])
    assert p6 and p7
    assert p6 == p7
    # unrelated commits have different patch-ids
    assert patch_id_for(repo, shas["c1"]) != p6


def test_full_diff_parseable(repo, shas):
    db = make_db(repo / "w.db", repo)
    walk(repo, db, "main")
    r = to_row(repo, shas["c3"], 3, shas["c2"])
    assert "alpha" in r.diff

    d = diff_for(repo, shas["c3"])
    assert "-alpha" in d and "+ALPHA" in d


def test_list_commits_reverse_chronological(repo, shas):
    got = list_commits(repo, "main")
    assert got[0] == shas["c1"]
    assert got[-1] == shas["c8"]


def test_merge_commit_diffs_vs_first_parent(repo, shas):
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "side", "HEAD~1"],
        check=True,
        capture_output=True,
    )
    (repo / "side.txt").write_text("side\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "side work"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "side", "-m", "merge side"],
        check=True,
        capture_output=True,
    )
    merge_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert commit_kind(repo, merge_sha) == "merge"
    diff = diff_for(repo, merge_sha)
    assert "side.txt" in diff
    db = make_db(repo / "w.db", repo)
    walk(repo, db, "main")
    row = db.execute("SELECT kind, diff FROM commits WHERE sha = ?", (merge_sha,)).fetchone()
    assert row["kind"] == "merge"


# ---------------------------------------------------------------- exclusion --
# ADR-0003 clause 4: paths under .skill-stats/ (any depth) are dropped from
# every commit's diff before storage; store-only commits are skipped entirely.


def test_exclusion_skips_store_only_and_strips_mixed(repo):
    c1 = commit_files(repo, "normal content", {"n.txt": "one\ntwo\n"})
    commit_files(repo, "artifact update", {".skill-stats/artifact.sql": "INSERT\n"})
    c3 = commit_files(
        repo,
        "mixed content",
        {
            "n.txt": "ONE\ntwo\n",
            ".skill-stats/report.html": "<html>\n",
        },
    )
    commit_files(repo, "nested artifact", {"sub/.skill-stats/x.txt": "x\n"})
    c5 = commit_files(repo, "near miss name", {".skill-stats-keep/c.txt": "c\n"})
    db = make_db(repo / "w.db", repo)
    assert walk(repo, db, "main") == 3
    rows = db.execute("SELECT sha, walk_index, diff FROM commits ORDER BY walk_index").fetchall()
    # c2 (store-only) and c4 (nested store-only) leave no rows at all
    assert [r["sha"] for r in rows] == [c1, c3, c5]
    # skipped commits leave walk_index gaps; ids/order of the rest stay stable
    assert [r["walk_index"] for r in rows] == [1, 3, 5]
    assert ".skill-stats" not in rows[1]["diff"]
    assert "n.txt" in rows[1]["diff"]
    assert ".skill-stats-keep" in rows[2]["diff"]


def test_churn_columns_match_diff_churn_rules(repo):
    c1 = commit_files(repo, "add three", {"n.txt": "one\ntwo\nthree\n"})
    c2 = commit_files(repo, "rewrite one", {"n.txt": "ONE\ntwo\nthree\n"})
    c3 = commit_files(
        repo,
        "mixed with store",
        {
            "n.txt": "ONE\nTWO\nthree\n",
            ".skill-stats/artifact.sql": "+INSERT\n+MORE\n",
        },
    )
    db = make_db(repo / "w.db", repo)
    walk(repo, db, "main")
    cols = {
        r["sha"]: (r["added_lines"], r["deleted_lines"], r["churn_lines"])
        for r in db.execute("SELECT sha, added_lines, deleted_lines, churn_lines FROM commits")
    }
    assert cols[c1] == (3, 0, 3)  # +++/--- headers never count
    assert cols[c2] == (1, 1, 2)  # -one +ONE
    assert cols[c3] == (1, 1, 2)  # store lines dropped before counting


def test_walk_resumes_across_skipped_commits(repo):
    commit_files(repo, "normal content", {"n.txt": "one\n"})
    commit_files(repo, "artifact update", {".skill-stats/artifact.sql": "INSERT\n"})
    commit_files(repo, "more content", {"n.txt": "one\ntwo\n"})
    db = make_db(repo / "w.db", repo)
    assert walk(repo, db, "main") == 2
    c4 = commit_files(repo, "tail", {"t.txt": "t\n"})
    assert walk(repo, db, "main") == 1
    row = db.execute("SELECT sha, walk_index FROM commits ORDER BY walk_index DESC").fetchone()
    assert row["sha"] == c4 and row["walk_index"] == 4


# ------------------------------------------------------------- diff parsing --


def test_prepare_diff_golden_counts():
    diff = (
        "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,2 @@\n-old\n+new\n ctx\n"
    )
    stat = prepare_diff(diff)
    assert (stat.added, stat.deleted, stat.churn) == (1, 1, 2)
    assert stat.diff == diff


def test_prepare_diff_counts_nothing_without_content():
    diff = "diff --git a/y.bin b/y.bin\nBinary files a/y.bin and b/y.bin differ\n"
    stat = prepare_diff(diff)
    assert (stat.added, stat.deleted, stat.churn) == (0, 0, 0)


def test_prepare_diff_strips_store_sections_any_depth():
    diff = (
        "diff --git a/n.txt b/n.txt\n"
        "--- a/n.txt\n"
        "+++ b/n.txt\n"
        "@@ -1 +1 @@\n"
        "-one\n"
        "+ONE\n"
        "diff --git a/.skill-stats/a.sql b/.skill-stats/a.sql\n"
        "--- a/.skill-stats/a.sql\n"
        "+++ b/.skill-stats/a.sql\n"
        "@@ -0,0 +1 @@\n"
        "+INSERT\n"
        "diff --git a/sub/.skill-stats/b.txt b/sub/.skill-stats/b.txt\n"
        "--- a/sub/.skill-stats/b.txt\n"
        "+++ b/sub/.skill-stats/b.txt\n"
        "@@ -0,0 +1 @@\n"
        "+nested\n"
        "diff --git a/.skill-stats-keep/c.txt b/.skill-stats-keep/c.txt\n"
        "--- a/.skill-stats-keep/c.txt\n"
        "+++ b/.skill-stats-keep/c.txt\n"
        "@@ -0,0 +1 @@\n"
        "+kept\n"
    )
    stat = prepare_diff(diff)
    assert ".skill-stats/a.sql" not in stat.diff
    assert "sub/.skill-stats" not in stat.diff
    assert "+nested" not in stat.diff
    assert ".skill-stats-keep" in stat.diff and "+kept" in stat.diff
    # only -one/+ONE/+kept count
    assert (stat.added, stat.deleted, stat.churn) == (2, 1, 3)


# --------------------------------------------- quoted diff headers (cl. 4) --
# git quotes BOTH header paths (C-style, octal escapes for non-ASCII) when
# either side needs it; exclusion must handle both spellings and reset state
# at every section boundary regardless of section order.


def test_parse_diff_header_bare_and_quoted():
    from skill_stats.gitwalk import parse_diff_header

    assert parse_diff_header("diff --git a/x.txt b/x.txt") == ("x.txt", "x.txt")
    assert parse_diff_header('diff --git "a/n\\303\\244me.txt" "b/n\\303\\244me.txt"') == (
        "näme.txt",
        "näme.txt",
    )
    assert parse_diff_header('diff --git "a/t\\t x" "b/t\\t x"') == ("t\t x", "t\t x")
    assert parse_diff_header('diff --git "a/.skill-stats/\\303\\244.sql" "b/.skill-stats/x"') == (
        ".skill-stats/ä.sql",
        ".skill-stats/x",
    )
    assert parse_diff_header("not a header") is None


def test_quoted_store_section_after_normal_does_not_leak(repo):
    c1 = commit_files(repo, "create", {"näme.txt": "one\ntwo\n", "n.txt": "one\ntwo\n"})
    c2 = commit_files(
        repo,
        "mixed quoted",
        {
            "näme.txt": "one\nTWO\n",
            "z/.skill-stats/ärger.sql": "INSERT\n",
        },
    )
    db = make_db(repo / "w.db", repo)
    assert walk(repo, db, "main") == 2
    row = db.execute(
        "SELECT diff, added_lines, deleted_lines, churn_lines FROM commits WHERE sha = ?", (c2,)
    ).fetchone()
    assert "+INSERT" not in row["diff"] and "rger.sql" not in row["diff"]
    assert "TWO" in row["diff"]
    assert row["diff"].count("diff --git") == 1  # only the normal section survives
    assert (row["added_lines"], row["deleted_lines"], row["churn_lines"]) == (1, 1, 2)
    assert c1  # fixture history shape


def test_quoted_store_section_before_normal_does_not_swallow(repo):
    commit_files(repo, "create", {"n.txt": "one\ntwo\n"})
    c2 = commit_files(
        repo,
        "mixed quoted",
        {
            ".skill-stats/ärger.sql": "INSERT\n",
            "n.txt": "one\nTWO\n",
        },
    )
    db = make_db(repo / "w.db", repo)
    assert walk(repo, db, "main") == 2
    row = db.execute(
        "SELECT diff, added_lines, deleted_lines, churn_lines FROM commits WHERE sha = ?", (c2,)
    ).fetchone()
    assert "+INSERT" not in row["diff"]
    assert "TWO" in row["diff"]  # the later normal section is kept, not swallowed
    assert (row["added_lines"], row["deleted_lines"], row["churn_lines"]) == (1, 1, 2)


def test_prepare_diff_resets_state_at_every_header():
    # handcrafted mixed order: bare normal, quoted store, bare normal
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        'diff --git "a/.skill-stats/\\303\\244.sql" "b/.skill-stats/\\303\\244.sql"\n'
        '--- "a/.skill-stats/\\303\\244.sql"\n'
        '+++ "b/.skill-stats/\\303\\244.sql"\n'
        "@@ -0,0 +1 @@\n"
        "+INSERT\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    stat = prepare_diff(diff)
    assert "+INSERT" not in stat.diff
    assert "+new" in stat.diff and "+y" in stat.diff  # section after store survives
    assert (stat.added, stat.deleted, stat.churn) == (2, 2, 4)
