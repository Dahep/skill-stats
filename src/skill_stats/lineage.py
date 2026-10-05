"""Fix lineage closure: fix -> fix -> ... -> original feature(s).

fixes_features stores one row per (fix, feature) reach with via_fix_commit_id
pointing at the first intermediate Fix in the chain (NULL when the first-hop
target unit defines the Feature directly). Attribution is attributed per
lineage: a fix targeting a fix of a feature counts the feature once.
"""

import sqlite3


def close_lineage(conn: sqlite3.Connection) -> int:
    """Compute/update the transitive closure for all fix commits.
    Chronological order guarantees a fix's targets have already been closed
    when it is processed. Idempotent (INSERT OR IGNORE)."""
    rows = conn.execute(
        """SELECT c.id FROM commits c
           JOIN fix_targets ft ON ft.fix_commit_id = c.id
           ORDER BY c.walk_index"""
    ).fetchall()
    added = 0
    with conn:
        for (fix_id,) in rows:
            added += close_one(conn, fix_id)
    return added


def close_one(conn: sqlite3.Connection, fix_id: int) -> int:
    added = 0
    seen: set[int] = set()
    queue: list[tuple[int, int | None]] = [
        (r["target_commit_id"], None)
        for r in conn.execute(
            "SELECT target_commit_id FROM fix_targets WHERE fix_commit_id = ?", (fix_id,)
        )
    ]
    while queue:
        unit, first_fix = queue.pop(0)
        if unit in seen or unit == fix_id:
            continue
        seen.add(unit)
        for r in conn.execute(
            "SELECT feature_id FROM commits_features WHERE commit_id = ? AND role = 'defines'",
            (unit,),
        ):
            cur = conn.execute(
                "INSERT OR IGNORE INTO fixes_features (fix_commit_id, feature_id,"
                " via_fix_commit_id) VALUES (?, ?, ?)",
                (fix_id, r["feature_id"], first_fix),
            )
            added += cur.rowcount
        verdict = conn.execute(
            "SELECT verdict FROM commit_verdicts WHERE commit_id = ?", (unit,)
        ).fetchone()
        if verdict and verdict["verdict"] == "fix":
            next_fix = first_fix if first_fix is not None else unit
            for r in conn.execute(
                "SELECT target_commit_id FROM fix_targets WHERE fix_commit_id = ?", (unit,)
            ):
                queue.append((r["target_commit_id"], next_fix))
    return added
