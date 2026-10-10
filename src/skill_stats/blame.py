"""Line-source attribution: which walk-commit's lines does a commit hit?

For a commit C (first-parent walk), look at deletions/rewrites vs first parent
P (design: blame -w -C at the parent to a commit that defines a feature or
another fix; merge commits only ever arrive through their first parent on the
Target branch).
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gitwalk import _git, is_store_path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


def changed_hunks(repo: Path, sha: str) -> dict[str, list[Hunk]]:
    """file -> hunks whose OLD-side lines exist at parent (b > 0)."""
    out = _git(repo, "diff", "--first-parent", "-U0", f"{sha}^..{sha}")
    per_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("diff --git "):
            m: re.Match[str] | None
            if m := re.match(r"diff --git a/(.*?) b/.*$", line):
                current = m.group(1)
                if is_store_path(current):
                    current = None  # store paths never contribute blame evidence
                if current is not None:
                    per_file.setdefault(current, [])
        elif current is not None:
            per_file[current].append(line)
    result: dict[str, list[Hunk]] = {}
    for file, lines in per_file.items():
        if file == "/dev/null" or not lines:
            continue
        if lines and any(line.startswith(("Binary files", "GIT binary patch")) for line in lines):
            continue
        text = "\n".join(lines)
        hunks = [
            Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) else 1,
            )
            for m in HUNK_RE.finditer(text)
        ]
        hunks = [h for h in hunks if h.old_count > 0]
        if hunks:
            result[file] = hunks
    return result


def delete_ranges(repo: Path, sha: str) -> dict[str, list[tuple[int, int]]]:
    """file -> (start_line, count) contiguous old-side deletion ranges."""
    return {
        file: [(h.old_start, h.old_count) for h in hunks]
        for file, hunks in changed_hunks(repo, sha).items()
    }


def blame_deleted_lines(repo: Path, sha: str) -> dict[str, list[str]]:
    """file -> per-old-line blamed shas, walk commits only (last toucher rule).
    Returns {} for files blame can't handle (binary etc.)."""
    ranges = delete_ranges(repo, sha)
    out: dict[str, list[str]] = {}
    for file, rs in ranges.items():
        per_line: list[str] = []
        try:
            for start, count in contiguous(rs):
                text = _git(
                    repo,
                    "blame",
                    "-w",
                    "-C",
                    "--first-parent",
                    "--porcelain",
                    "-L",
                    f"{start},{start + count - 1}",
                    f"{sha}^",
                    "--",
                    file,
                )
                per_line.extend(_porcelain_shas(text)[:count])
        except subprocess.CalledProcessError:
            continue
        if per_line:
            out[file] = per_line
    return out


def contiguous(rs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge ranges whose endpoints are adjacent (start_i + count_i == next start)."""
    merged: list[tuple[int, int]] = []
    for start, count in sorted(rs):
        if merged and merged[-1][0] + merged[-1][1] == start:
            merged[-1] = (merged[-1][0], merged[-1][1] + count)
        else:
            merged.append((start, count))
    return merged


SHA_LINE_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")


def _porcelain_shas(text: str) -> list[str]:
    """One sha per blamed old line, in order, from git blame --porcelain v1."""
    shas: list[str] = []
    for line in text.splitlines():
        if line.startswith(
            ("\t", "author ", "committer ", "summary", "boundary", "filename ", "previous ")
        ):
            continue
        m = SHA_LINE_RE.match(line)
        if m:
            shas.append(m.group(1))
    return shas
