"""Reduction: blame evidence -> fix_touches / fix_targets rows."""

import sqlite3
from pathlib import Path

from .blame import blame_deleted_lines


def annotate_fix(repo: Path, conn: sqlite3.Connection, sha: str, min_target_lines: int = 1) -> None:
    """Record fix_touches and fix_targets for one fix verdict commit."""
    hits_per_source: dict[str, int] = {}
    excluded = {sha}
    row = conn.execute("SELECT id, parent_sha FROM commits WHERE sha = ?", (sha,)).fetchone()
    if not row or not row["parent_sha"]:
        return
    per_file = blame_deleted_lines(repo, sha)
    for _file, shas in per_file.items():
        for source in shas:
            if source in excluded or source.startswith("0000"):
                continue
            hits_per_source[source] = hits_per_source.get(source, 0) + 1

    commit_id = row["id"]
    prev_targets = {
        t["target_commit_id"]
        for t in conn.execute(
            "SELECT target_commit_id FROM fix_targets WHERE fix_commit_id = ?", (commit_id,)
        )
    }
    touched: list[tuple[str, int]] = sorted(hits_per_source.items(), key=lambda kv: -kv[1])
    with conn:
        conn.execute("DELETE FROM fix_touches WHERE fix_commit_id = ?", (commit_id,))
        for source_sha, lines in touched:
            src = conn.execute("SELECT id FROM commits WHERE sha = ?", (source_sha,)).fetchone()
            if not src:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fix_touches (fix_commit_id, source_commit_id, hit_lines)"
                " VALUES (?, ?, ?)",
                (commit_id, src["id"], lines),
            )
            if lines >= min_target_lines and src["id"] not in prev_targets:
                conn.execute(
                    "INSERT OR IGNORE INTO fix_targets (fix_commit_id, target_commit_id)"
                    " VALUES (?, ?)",
                    (commit_id, src["id"]),
                )


def annotate_all(repo: Path, conn: sqlite3.Connection, min_target_lines: int = 1) -> int:
    shas = [
        r["sha"] for r in conn.execute(
            """SELECT c.sha FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = 'fix' ORDER BY c.walk_index"""
        )
    ]
    for sha in shas:
        annotate_fix(repo, conn, sha, min_target_lines)
    return len(shas)
