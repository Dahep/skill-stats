"""Database handle: open/create and apply schema migrations."""

import sqlite3
from collections.abc import Callable
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
        if applied:
            continue
        if callable(step):
            _apply_migration(conn, step, tablename)
        else:
            conn.executescript(step)
            conn.execute(f"CREATE TABLE {tablename} (ok)")
            conn.commit()
    return conn


def _apply_migration(
    conn: sqlite3.Connection, migrate: Callable[[sqlite3.Connection], None], tablename: str
) -> None:
    """Apply one migration step atomically.

    The explicit transaction covers ALL of the step's DDL and DML plus this
    bookkeeping marker (python's sqlite3 autocommits DDL outside a real
    transaction, so `with conn:` alone is not enough), so a mid-migration
    failure rolls back to the previous shape and leaves the DB reopenable —
    the step then re-runs cleanly. FK enforcement is toggled outside the
    transaction (the pragma is a no-op inside one) because steps may rebuild
    parent tables."""
    conn.commit()  # end any implicit transaction so BEGIN/pragma take effect
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        try:
            migrate(conn)
            conn.execute(f"CREATE TABLE {tablename} (ok)")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
