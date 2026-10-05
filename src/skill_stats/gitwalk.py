"""Walk the Target branch and import commits into the DB.

Scope: first-parent walk of the Target branch (main), chronological. Each
commit covers a squash merge as a single main-branch commit. Patch-id captures
the commit's diff so later duplicate/patch-equivalent content can be detected.
"""

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass
class CommitRow:
    sha: str
    parent_sha: str | None
    tree_sha: str
    committed_at: str
    author_name: str
    title: str
    message: str
    patch_id: str | None
    walk_index: int
    kind: str  # 'normal' | 'merge'
    diff: str


def _git(repo: Path, *args: str, text: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return cast(str, out.stdout)


def current_branch(repo: Path) -> str:
    out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip()


def list_commits(repo: Path, branch: str) -> list[str]:
    """First-parent chronological shas; root last."""
    out = _git(repo, "rev-list", "--first-parent", "--reverse", branch)
    return [s for s in out.splitlines() if s]


def commit_kind(repo: Path, sha: str) -> str:
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    return "merge" if len(parents) > 2 else "normal"


def diff_for(repo: Path, sha: str) -> str:
    """Unified diff versus first parent (empty diff for root)."""
    if _parents(repo, sha) == 0:
        return _git(repo, "show", "--format=", sha)
    return _git(repo, "diff", f"{sha}^..{sha}")


def _parents(repo: Path, sha: str) -> int:
    out = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    return max(0, len(out) - 1)


def patch_id_for(repo: Path, sha: str) -> str | None:
    """git patch-id of the stable diff. Returns None if diff is empty."""
    diff = diff_for(repo, sha)
    if not diff.strip():
        return None
    p = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=diff,
        capture_output=True,
        text=True,
        cwd=repo,
        check=True,
    )
    token = p.stdout.split()
    return token[0] if token else None


def tree_sha(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}").strip()


def author_and_time(repo: Path, sha: str) -> tuple[str, str]:
    out = _git(repo, "show", "-s", "--format=%an%x1f%aI", sha).strip().split("\x1f")
    return out[0], out[1]


def commit_message(repo: Path, sha: str) -> tuple[str, str]:
    """(title, full message) - title = subject line."""
    raw = _git(repo, "show", "-s", "--format=%s%x1f%B", sha)
    subject, body = raw.split("\x1f", 1)
    return subject.strip(), body.strip()


def to_row(repo: Path, sha: str, walk_index: int, parent_sha: str | None) -> CommitRow:
    kind = commit_kind(repo, sha)
    author, committed_at = author_and_time(repo, sha)
    title, message = commit_message(repo, sha)
    return CommitRow(
        sha=sha,
        parent_sha=parent_sha,
        tree_sha=tree_sha(repo, sha),
        committed_at=committed_at,
        author_name=author,
        title=title,
        message=message,
        patch_id=patch_id_for(repo, sha),
        walk_index=walk_index,
        kind=kind,
        diff=diff_for(repo, sha),
    )


WALK_CHUNK = 200


def walk(repo: Path, conn: sqlite3.Connection, branch: str | None = None) -> int:
    """Import commits in walk order; resumable via walk_index. Returns count added."""
    branch = branch or current_branch(repo)
    shas = list_commits(repo, branch)
    added = 0
    max_walk = conn.execute(
        "SELECT COALESCE(MAX(walk_index), 0) FROM commits"
    ).fetchone()[0]
    with conn:
        for idx, sha in enumerate(shas, start=1):
            expected_parent = shas[idx - 2] if idx >= 2 else None
            if idx <= max_walk:
                existing = conn.execute(
                    "SELECT sha FROM commits WHERE walk_index = ?", (idx,)
                ).fetchone()
                if existing["sha"] != sha:
                    raise RuntimeError(
                        f"rewritten history at walk_index={idx}: DB has {existing['sha']},"
                        f" git has {sha}. Re-init the DB or rewind manually."
                    )
                continue
            parent = expected_parent
            row = to_row(repo, sha, idx, parent)
            conn.execute(
                """INSERT INTO commits
                   (sha, parent_sha, tree_sha, committed_at, author_name, title, message,
                    subject_patch_id, walk_index, kind, diff)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row.sha, row.parent_sha, row.tree_sha, row.committed_at, row.author_name,
                 row.title, row.message, row.patch_id, row.walk_index, row.kind, row.diff),
            )
            added += 1
    return added
