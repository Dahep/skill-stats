"""Database handle: open/create and apply schema migrations."""

import sqlite3
from pathlib import Path

from .schema import SCHEMA_STEPS


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for i, step in enumerate(SCHEMA_STEPS):
        tablename = f"_schema_step_{i:04d}"
        applied = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (tablename,)
        ).fetchone()
        if not applied:
            conn.executescript(step)
            conn.execute(f"CREATE TABLE {tablename} (ok)")
            conn.commit()
    return conn
