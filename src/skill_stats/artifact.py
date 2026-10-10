"""The committed Artifact: a deterministic whole-snapshot SQL text (ADR-0003).

Format (version 1) — self-executing: ``sqlite3 db < skill-stats.sql`` rebuilds
a standalone queryable DB from any clone.

* Header comment lines, in this order: ``-- skill-stats artifact``,
  ``-- format: 1``, ``-- schema-step: <n>``, ``-- repository-key: <key>``,
  ``-- covered-through: <sha>`` (latest walked Target-branch commit included)
  and finally ``-- digest: <sha256>`` — the Artifact digest of CONTEXT.md:
  sha256 over the whole content with the digest line's own value blanked
  (the line becomes exactly ``-- digest:``). Unkeyed checksum, honest limits:
  detects accidental or manual edits, not adversarial forgery (ADR-0003).
* Then per table: its CREATE TABLE (curated shape — column types and
  constraints kept, cross-table REFERENCES dropped so the dump materializes
  standalone) followed by one canonical INSERT per row in stable order.

Curated projection (ADR-0003 clause 2), the artifact is not a byte copy of
the working DB: table ``runs`` is excluded entirely; settings rows other than
``branch``/``model``/``min_target_lines``/``unattributed_lines`` are excluded
(esp. ``repo`` — a machine path — and ``classify_session_id``); wall-clock
columns are frozen to sentinels (``repositories.added_at`` -> ''); the
``commits.diff`` payload is machine-derivable and inserted as '' (kept in the
DDL so shapes match); run-provenance columns (``commit_verdicts.run_id``,
``features.created_run_id``) insert NULL. ``classified_at`` IS kept: verdicts
are classified once, so the value is stable audit data — a re-classification
changing it is an honest data change, not churn.

Determinism is the point (the digest must be stable across machines): every
table is ordered by its stored keys (autoincrement ids / UNIQUE keys), quoting
is fixed, and nothing wall-clock-dependent reaches the output.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import htmlreport

FORMAT = 1

DIGEST_BLANK = "-- digest:"
_DIGEST_LINE_RE = re.compile(rb"^-- digest:(?: ([0-9a-f]+))?$", re.M)

_SETTINGS_WHITELIST = (
    "backfill_state",
    "branch",
    "min_target_lines",
    "model",
    "unattributed_lines",
)


@dataclass(frozen=True)
class _Table:
    name: str
    ddl: str
    columns: tuple[str, ...]
    source: str  # SELECT <columns> FROM ... ORDER BY <stable keys>
    sentinels: dict[str, object] = field(default_factory=dict)


_TABLES: tuple[_Table, ...] = (
    _Table(
        "repositories",
        """CREATE TABLE repositories (
               id INTEGER PRIMARY KEY,
               repo_key TEXT NOT NULL UNIQUE,
               slug TEXT NOT NULL,
               branch TEXT,
               clone_url TEXT,
               added_at TEXT
           );""",
        ("id", "repo_key", "slug", "branch", "clone_url", "added_at"),
        "SELECT id, repo_key, slug, branch, clone_url, added_at FROM repositories ORDER BY id",
        {"added_at": ""},  # wall-clock -> sentinel (impl-w1-hazard obligation)
    ),
    _Table(
        "commits",
        """CREATE TABLE commits (
               id INTEGER PRIMARY KEY,
               repo_id INTEGER NOT NULL,
               sha TEXT NOT NULL,
               parent_sha TEXT,
               tree_sha TEXT NOT NULL,
               committed_at TEXT NOT NULL,
               author_name TEXT NOT NULL,
               title TEXT NOT NULL,
               message TEXT NOT NULL,
               subject_patch_id TEXT,
               walk_index INTEGER NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
               diff TEXT NOT NULL,
               added_lines INTEGER NOT NULL DEFAULT 0,
               deleted_lines INTEGER NOT NULL DEFAULT 0,
               churn_lines INTEGER NOT NULL DEFAULT 0,
               UNIQUE (repo_id, sha),
               UNIQUE (repo_id, walk_index)
           );""",
        (
            "id",
            "repo_id",
            "sha",
            "parent_sha",
            "tree_sha",
            "committed_at",
            "author_name",
            "title",
            "message",
            "subject_patch_id",
            "walk_index",
            "kind",
            "diff",
            "added_lines",
            "deleted_lines",
            "churn_lines",
        ),
        """SELECT id, repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title,
                  message, subject_patch_id, walk_index, kind, diff, added_lines,
                  deleted_lines, churn_lines
           FROM commits ORDER BY id""",
        {"diff": ""},  # raw diff text is machine-derivable, never committed
    ),
    _Table(
        "commit_verdicts",
        """CREATE TABLE commit_verdicts (
               commit_id INTEGER PRIMARY KEY,
               verdict TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor',
                                                        'revert', 'cleanup', 'merge',
                                                        'unknown')),
               rationale TEXT NOT NULL,
               raw_llm_output TEXT NOT NULL,
               model TEXT NOT NULL,
               run_id INTEGER,
               classified_at TEXT NOT NULL
           );""",
        ("commit_id", "verdict", "rationale", "raw_llm_output", "model", "run_id", "classified_at"),
        """SELECT commit_id, verdict, rationale, raw_llm_output, model, run_id, classified_at
           FROM commit_verdicts ORDER BY commit_id""",
        {"run_id": None},  # run provenance is machine-local
    ),
    _Table(
        "features",
        """CREATE TABLE features (
               id TEXT PRIMARY KEY,
               title TEXT NOT NULL,
               about TEXT NOT NULL DEFAULT '',
               title_history_json TEXT NOT NULL DEFAULT '[]',
               created_at TEXT NOT NULL,
               created_run_id INTEGER,
               repo_id INTEGER NOT NULL,
               live_lines INTEGER NOT NULL DEFAULT 0
           );""",
        (
            "id",
            "title",
            "about",
            "title_history_json",
            "created_at",
            "created_run_id",
            "repo_id",
            "live_lines",
        ),
        """SELECT id, title, about, title_history_json, created_at, created_run_id,
                  repo_id, live_lines
           FROM features ORDER BY id""",
        {"created_run_id": None},
    ),
    _Table(
        "commits_features",
        """CREATE TABLE commits_features (
               commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
               PRIMARY KEY (commit_id, feature_id),
               UNIQUE (feature_id, commit_id)
           );""",
        ("commit_id", "feature_id", "role"),
        "SELECT commit_id, feature_id, role FROM commits_features ORDER BY commit_id, feature_id",
    ),
    _Table(
        "fix_touches",
        """CREATE TABLE fix_touches (
               fix_commit_id INTEGER NOT NULL,
               source_commit_id INTEGER NOT NULL,
               hit_lines INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, source_commit_id)
           );""",
        ("fix_commit_id", "source_commit_id", "hit_lines"),
        "SELECT fix_commit_id, source_commit_id, hit_lines FROM fix_touches"
        " ORDER BY fix_commit_id, source_commit_id",
    ),
    _Table(
        "fix_targets",
        """CREATE TABLE fix_targets (
               fix_commit_id INTEGER NOT NULL,
               target_commit_id INTEGER NOT NULL,
               PRIMARY KEY (fix_commit_id, target_commit_id)
           );""",
        ("fix_commit_id", "target_commit_id"),
        "SELECT fix_commit_id, target_commit_id FROM fix_targets"
        " ORDER BY fix_commit_id, target_commit_id",
    ),
    _Table(
        "fixes_features",
        """CREATE TABLE fixes_features (
               fix_commit_id INTEGER NOT NULL,
               feature_id TEXT NOT NULL,
               via_fix_commit_id INTEGER,
               PRIMARY KEY (fix_commit_id, feature_id),
               UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
           );""",
        ("fix_commit_id", "feature_id", "via_fix_commit_id"),
        "SELECT fix_commit_id, feature_id, via_fix_commit_id FROM fixes_features"
        " ORDER BY fix_commit_id, feature_id, via_fix_commit_id",
    ),
    _Table(
        "feature_line_samples",
        """CREATE TABLE feature_line_samples (
               feature_id TEXT NOT NULL,
               at_commit_sha TEXT NOT NULL,
               live_lines INTEGER NOT NULL,
               PRIMARY KEY (feature_id, at_commit_sha)
           );""",
        ("feature_id", "at_commit_sha", "live_lines"),
        "SELECT feature_id, at_commit_sha, live_lines FROM feature_line_samples"
        " ORDER BY feature_id, at_commit_sha",
    ),
    _Table(
        "settings",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        ("key", "value"),
        "SELECT key, value FROM settings WHERE key IN"
        f" ({', '.join(f'{k!r}' for k in _SETTINGS_WHITELIST)}) ORDER BY key",
    ),
)


def _lit(value: object) -> str:
    """Fixed SQL literal quoting (strings single-quoted, ints bare, NULL as NULL)."""
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# printable ASCII minus the escape char: header values made only of these stay
# literal; every other byte is escaped as \xNN (the escape char itself too, so
# the representation is injective)
_HDR_SAFE = set(range(0x20, 0x7F)) - {ord("\\")}


def _hdr(value: object) -> str:
    """Guaranteed single-line ASCII-safe header representation. A value can
    never break out of its ``-- ...`` comment line: newlines, control bytes and
    non-ASCII become \\xNN escapes (SQL/dot-command injection via a crafted
    repository key is structurally impossible); values needing no escaping
    stay literal for readability."""
    out = []
    for b in str(value).encode("utf-8"):
        out.append(chr(b) if b in _HDR_SAFE else f"\\x{b:02X}")
    return "".join(out)


def _head(conn: sqlite3.Connection) -> str:
    covered = conn.execute("SELECT sha FROM commits ORDER BY walk_index DESC LIMIT 1").fetchone()
    key = conn.execute("SELECT repo_key FROM repositories ORDER BY id LIMIT 1").fetchone()
    step = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_schema_step_%'"
        " ORDER BY name DESC LIMIT 1"
    ).fetchone()
    step_n = int(step["name"].rsplit("_", 1)[-1]) if step else 0
    return (
        "-- skill-stats artifact\n"
        f"-- format: {FORMAT}\n"
        f"-- schema-step: {_hdr(step_n)}\n"
        f"-- repository-key: {_hdr(key['repo_key'] if key else '')}\n"
        f"-- covered-through: {_hdr(covered['sha'] if covered else '')}\n"
        f"{DIGEST_BLANK}\n"
    )


def serialize(conn: sqlite3.Connection) -> bytes:
    """The complete canonical artifact content, digest line filled in."""
    parts = [_head(conn)]
    for table in _TABLES:
        parts.append(table.ddl.strip() + "\n")
        cols = ", ".join(table.columns)
        for row in conn.execute(table.source):
            values = ", ".join(
                _lit(table.sentinels[c]) if c in table.sentinels else _lit(row[c])
                for c in table.columns
            )
            parts.append(f"INSERT INTO {table.name} ({cols}) VALUES ({values});\n")
    blank = "".join(parts)
    digest = hashlib.sha256(blank.encode("utf-8")).hexdigest()
    content = blank.replace(f"{DIGEST_BLANK}\n", f"{DIGEST_BLANK} {digest}\n", 1)
    return content.encode("utf-8")


def write(conn: sqlite3.Connection, repo_dir: Path, with_report: bool = True) -> Path:
    """Land the artifact: ``.skill-stats/skill-stats.sql`` plus, by default, the
    regenerated ``report.html`` (both read the WORKING db — settings.repo is a
    machine path and absent from the artifact)."""
    store = Path(repo_dir) / ".skill-stats"
    store.mkdir(parents=True, exist_ok=True)
    if with_report:
        htmlreport.write_report(conn, store / "report.html")
    path = store / "skill-stats.sql"
    path.write_bytes(serialize(conn))
    return path


def digest_of(path: Path) -> str:
    """The stored Artifact digest's meaning: sha256 over the exact file bytes
    with the digest line's value blanked. Raises when the file carries no
    digest. Bytes-based: no decoding happens anywhere in the path."""
    parts = _digest_parts(Path(path).read_bytes())
    if parts is None:
        raise ValueError(f"{path}: no digest line")
    return hashlib.sha256(parts[0]).hexdigest()


def verify(path: Path) -> str | None:
    """None when the stored digest covers the file's exact bytes; else the
    error. Works on raw bytes — CRLF or invalid-UTF-8 tampering can never
    launder through text-mode normalization, and malformed content yields a
    failure verdict instead of an exception."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return str(exc)
    parts = _digest_parts(data)
    if parts is None:
        return "no digest line"
    blanked, stored = parts
    recomputed = hashlib.sha256(blanked).hexdigest()
    if recomputed != stored.decode("ascii"):
        return f"digest mismatch: stored {stored.decode('ascii')}, recomputed {recomputed}"
    return None


def _digest_parts(data: bytes) -> tuple[bytes, bytes] | None:
    """(content with the digest value blanked, stored digest bytes) from raw
    bytes; None when the digest line is missing or malformed."""
    m = _DIGEST_LINE_RE.search(data)  # first occurrence: the header, always first
    if m is None or not m.group(1):
        return None
    blanked = data[: m.start()] + DIGEST_BLANK.encode("ascii") + data[m.end() :]
    return blanked, m.group(1)
