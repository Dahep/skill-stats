"""Walk + reconcile tests."""

import pytest

from skill_stats.db import connect
from skill_stats.gitwalk import commit_kind, diff_for, list_commits, patch_id_for, to_row, walk


def test_walk_imports_in_order(repo, shas):
    db = connect(repo / "w.db")
    n = walk(repo, db, "main")
    assert n == 9
    rows = db.execute(
        "SELECT sha, walk_index, parent_sha FROM commits ORDER BY walk_index"
    ).fetchall()
    assert [r["sha"] for r in rows] == list(shas.values())
    assert rows[0]["parent_sha"] is None
    assert rows[3]["parent_sha"] == shas["c3"]


def test_walk_is_resumable(repo, shas):
    db = connect(repo / "w.db")
    assert walk(repo, db, "main") == 9
    # appending a commit then walking adds exactly the new one
    (repo / "n.txt").write_text("n\n")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "new tail"], check=True)
    assert walk(repo, db, "main") == 1
    assert db.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 10


def test_walk_detects_rewritten_history(repo, shas):
    db = connect(repo / "w.db")
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
    db = connect(repo / "w.db")
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

    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "side", "HEAD~1"], check=True,
                   capture_output=True)
    (repo / "side.txt").write_text("side\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "side work"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "side", "-m", "merge side"],
                   check=True, capture_output=True)
    merge_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                               capture_output=True, text=True).stdout.strip()
    assert commit_kind(repo, merge_sha) == "merge"
    diff = diff_for(repo, merge_sha)
    assert "side.txt" in diff
    db = connect(repo / "w.db")
    walk(repo, db, "main")
    row = db.execute("SELECT kind, diff FROM commits WHERE sha = ?", (merge_sha,)).fetchone()
    assert row["kind"] == "merge"
