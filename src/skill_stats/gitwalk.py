"""Walk the Target branch and import commits into the DB.

Scope: first-parent walk of the Target branch (main), chronological. Each
commit covers a squash merge as a single main-branch commit. Patch-id captures
the commit's diff so later duplicate/patch-equivalent content can be detected.

Exclusion (ADR-0003 clause 4): paths under ``.skill-stats/`` (any depth) are
dropped from every commit's diff before storage; a commit whose stripped diff
is empty is skipped entirely and nothing about it is stored. The rule is a
path pattern, so re-inits, fork history, and CI-created commits are covered.

Wave 1: a DB holds exactly one repository (the row it was init'd against), so
walk_index is unique within that repo. The schema is multi-repo-ready
(UNIQUE (repo_id, sha) / (repo_id, walk_index)); the CLI is not yet.
"""

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from . import identity

STORE_DIR_NAME = ".skill-stats"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")


def is_store_path(path: str) -> bool:
    """True when any path component names the artifact store dir (any depth)."""
    return STORE_DIR_NAME in PurePosixPath(path).parts


@dataclass(frozen=True)
class DiffStat:
    """A diff with store-path sections removed plus its line counts."""

    diff: str
    added: int
    deleted: int
    churn: int


def prepare_diff(diff: str) -> DiffStat:
    """One pass over a unified diff: drop sections touching store paths and
    count added/deleted lines (rules of the old metrics._diff_churn: lines
    starting +/- except the +++/--- headers). The single source of the churn
    math; churn_per_feature reads the stored columns derived from it."""
    out: list[str] = []
    added = deleted = 0
    keep = True  # lines before the first file header stay with the diff
    for line in diff.splitlines(keepends=True):
        header = _DIFF_HEADER_RE.match(line.rstrip("\r\n"))
        if header:
            keep = not (is_store_path(header.group(1)) or is_store_path(header.group(2)))
        if not keep:
            continue
        out.append(line)
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return DiffStat("".join(out), added, deleted, added + deleted)


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
    """Import commits in walk order; resumable via walk_index. Returns count added.

    Commits whose stripped diff is empty (store-only) are skipped with a
    walk_index gap and store nothing (ADR-0003 clause 4)."""
    branch = branch or current_branch(repo)
    repo_id = identity.repo_id_for(conn)
    shas = list_commits(repo, branch)
    added = 0
    max_walk = conn.execute("SELECT COALESCE(MAX(walk_index), 0) FROM commits").fetchone()[0]
    with conn:
        for idx, sha in enumerate(shas, start=1):
            expected_parent = shas[idx - 2] if idx >= 2 else None
            if idx <= max_walk:
                existing = conn.execute(
                    "SELECT sha FROM commits WHERE walk_index = ?", (idx,)
                ).fetchone()
                if existing is None:
                    # gap: skipped as store-only at insert time. It must still
                    # strip to nothing; anything else means rewritten history.
                    if prepare_diff(diff_for(repo, sha)).diff.strip():
                        raise RuntimeError(
                            f"rewritten history at walk_index={idx}: DB skipped {sha}"
                            f" but it now has a non-store diff. Re-init the DB."
                        )
                    continue
                if existing["sha"] != sha:
                    raise RuntimeError(
                        f"rewritten history at walk_index={idx}: DB has {existing['sha']},"
                        f" git has {sha}. Re-init the DB or rewind manually."
                    )
                continue
            parent = expected_parent
            row = to_row(repo, sha, idx, parent)
            stat = prepare_diff(row.diff)
            if not stat.diff.strip():
                continue  # store-only commit: skipped, nothing stored
            conn.execute(
                """INSERT INTO commits
                   (repo_id, sha, parent_sha, tree_sha, committed_at, author_name, title,
                    message, subject_patch_id, walk_index, kind, diff, added_lines,
                    deleted_lines, churn_lines)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    repo_id,
                    row.sha,
                    row.parent_sha,
                    row.tree_sha,
                    row.committed_at,
                    row.author_name,
                    row.title,
                    row.message,
                    row.patch_id,
                    row.walk_index,
                    row.kind,
                    stat.diff,
                    stat.added,
                    stat.deleted,
                    stat.churn,
                ),
            )
            added += 1
    return added
