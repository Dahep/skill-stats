"""Repository identity: the Repository key of CONTEXT.md (canonical clone URL +
Target branch). Machine-local paths are NEVER identity; when the origin URL is
unavailable the key falls back to a documented ``local:`` shim placeholder.
URL normalization follows the ssh-to-https mapping idea of
htmlreport._repo_url (kept separate: that one is github-only display logic).
"""

import json
import re
import sqlite3
import subprocess
import warnings
from pathlib import Path

_SCP_RE = re.compile(r"^(?:[^@/:]+@)?([^/:]+):(.+)$")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_clone_url(remote: str) -> str | None:
    """Canonical https form of a clone URL: ssh spellings map to https, the
    trailing .git is stripped, the host is lowercased. Machine-local paths and
    file:// URLs return None (never identity)."""
    url = (remote or "").strip()
    if not url:
        return None
    if "://" in url:
        scheme, _, rest = url.partition("://")
        if scheme.lower() not in ("http", "https", "git", "ssh"):
            return None  # file:// and unknown schemes are not identity
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]
        return _https(host, path)
    m = _SCP_RE.match(url)  # scp-like ssh spelling: [user@]host:path
    if m:
        return _https(m.group(1), m.group(2))
    return None  # plain local path


def _https(host: str, path: str) -> str:
    return f"https://{host.lower()}/{path.strip('/').removesuffix('.git')}"


def slug_from_clone_url(url: str) -> str:
    """Repo slug from a canonical clone URL: path components joined by '-'."""
    path = url.partition("://")[2].partition("/")[2]
    parts = [p for p in path.split("/") if p]
    return _slug("-".join(parts)) or "repo"


def local_identity(path: str) -> tuple[str, str]:
    """(key base, slug) shim for a machine-local repo path: 'local:<basename>'
    plus the sanitized basename. A placeholder, not a real identity."""
    name = Path(path).name or "repo"
    return f"local:{name}", _slug(name) or "repo"


def repo_key(base: str, branch: str | None) -> str:
    """Repository key = clone URL (or shim base) plus the Target branch."""
    return f"{base}@{branch or ''}"


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def origin_url(repo: Path) -> str | None:
    """The repo's origin URL, or None when git cannot answer."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def register_repository(conn: sqlite3.Connection, repo: Path, branch: str | None) -> int:
    """Create or update THE repositories row for this DB from the repo's origin
    URL (local: shim when unavailable) and store its id in settings.repo_id so
    subsequent commands reuse it.

    Re-init safety: an existing row is never silently relabeled. Same key is an
    idempotent no-op; a key change against a DB with data refuses (or upgrades
    a shim to its canonical URL — the documented migration flow) and a
    canonical key is never downgraded to the local: shim because origin
    resolution failed on this run.

    Wave 1: a DB manages exactly one repository (the row the DB was init'd
    against); a second row is refused rather than silently half-supported."""
    url = origin_url(repo)
    canon = normalize_clone_url(url) if url else None
    if canon:
        base, slug, clone_url = canon, slug_from_clone_url(canon), canon
    else:
        base, slug, clone_url = *local_identity(str(repo)), None
    key = repo_key(base, branch)
    existing = conn.execute("SELECT id, repo_key FROM repositories ORDER BY id").fetchall()
    if len(existing) > 1:
        raise RuntimeError("wave 1 manages a single repository row per DB")
    with conn:
        if existing:
            row = existing[0]
            rid = int(row["id"])
            if key != row["repo_key"]:
                rid = _relabel(conn, rid, str(row["repo_key"]), key, slug, branch, clone_url)
        else:
            cur = conn.execute(
                "INSERT INTO repositories (repo_key, slug, branch, clone_url, added_at)"
                " VALUES (?, ?, ?, ?, datetime('now'))",
                (key, slug, branch, clone_url),
            )
            assert cur.lastrowid is not None
            rid = int(cur.lastrowid)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('repo_id', ?)",
            (json.dumps(rid),),
        )
    return rid


def _relabel(
    conn: sqlite3.Connection,
    rid: int,
    old_key: str,
    key: str,
    slug: str,
    branch: str | None,
    clone_url: str | None,
) -> int:
    """Apply a repository-key change to the single existing row, or refuse it."""
    old_canonical = not old_key.startswith("local:")
    if key.startswith("local:") and old_canonical:
        warnings.warn(
            f"keeping stored repository identity {old_key}: origin URL unavailable,"
            f" not downgrading to the {key} shim",
            UserWarning,
            stacklevel=2,
        )
        return rid
    has_data = bool(conn.execute("SELECT 1 FROM commits LIMIT 1").fetchone())
    if has_data and (old_canonical or key.startswith("local:")):
        raise RuntimeError(
            f"refusing to relabel repository identity: the DB holds commits under"
            f" {old_key} but init resolves {key}. Re-init against a different"
            f" repository or branch is not supported; use a fresh DB instead."
        )
    warnings.warn(f"replacing repository identity {old_key} -> {key}", UserWarning, stacklevel=2)
    conn.execute(
        "UPDATE repositories SET repo_key = ?, slug = ?, branch = ?, clone_url = ? WHERE id = ?",
        (key, slug, branch, clone_url, rid),
    )
    return rid


def repo_id_for(conn: sqlite3.Connection) -> int:
    """The DB's repository id: settings.repo_id when present (written by
    init/walk), else the lone repositories row (migration shim). Wave 1: one
    repository per DB; multi-repo routing arrives with the gather/CLI wave."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'repo_id'").fetchone()
    if row:
        rid = int(json.loads(row["value"]))
        if conn.execute("SELECT 1 FROM repositories WHERE id = ?", (rid,)).fetchone():
            return rid
    rows = conn.execute("SELECT id FROM repositories ORDER BY id").fetchall()
    if len(rows) == 1:
        return int(rows[0]["id"])
    if not rows:
        raise RuntimeError("no repository registered; run init first")
    raise RuntimeError("multiple repositories in one DB is not supported in wave 1")
