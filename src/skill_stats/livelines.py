"""Live lines: per-feature line counts at the Target-branch tip via blame.

Every line of every tracked file at the analyzed tip is owned by exactly one
commit (``git blame -w -C --porcelain``); commit lines accrue to the Features
the commit is attributed to (grill-r3-Q7): directly via commits_features
(defines/touches), and for fix commits through the fixes_features lineage
closure — so fix-owned lines accrue to the features the fix reaches, and a
commit claimed by two features counts its lines in both. Lines whose
introducing commit is not in the DB (work outside the walk) are counted and
surfaced as unattributed_lines — never silently dropped. Store paths
(``.skill-stats/**``) and submodule gitlinks are skipped.

History: ``feature_line_samples`` holds one Sample per (feature, sample
commit) for every feature alive at that commit (CONTEXT.md "Sample"). The
forward sample is written at the tip on every run (idempotent on the same
sha). Backfill (grill-r3-Q9: "Full backfill unless the repo is huge, sample in
that case") runs once, when the samples table is empty: a full sweep over
every walked commit, or ~BACKFILL_SAMPLE_POINTS evenly spaced commits when
the walk exceeds BACKFILL_FULL_MAX_COMMITS. A feature not yet defined at a
sample point simply has no row there.

The measured point is the latest WALKED Target-branch commit — identical to
the branch tip whenever walk ran first (update runs walk before live-lines).
"""

import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .blame import _porcelain_shas
from .gitwalk import _git, is_store_path

# Backfill plan caps (grill-r3-Q9 leaves the cap to implementation):
# a full sweep up to this many walked commits, evenly sampled above it.
BACKFILL_FULL_MAX_COMMITS = 400
BACKFILL_SAMPLE_POINTS = 160


@dataclass(frozen=True)
class LiveLineResult:
    """What one live-lines run measured and recorded."""

    at_sha: str  # the measured tip (latest walked commit)
    unattributed_lines: int  # tip lines whose commit is not in the DB
    samples_written: int  # feature_line_samples rows written (incl. tip)
    backfilled_points: int  # sample points processed by the backfill sweep


def sample_points(walk_indexes: list[int]) -> list[int]:
    """Backfill plan over walked commits: the full sweep under the size cap,
    otherwise ~BACKFILL_SAMPLE_POINTS evenly spaced points including the first
    and last commit. Deterministic for a given walk."""
    idx = sorted(walk_indexes)
    if len(idx) <= BACKFILL_FULL_MAX_COMMITS:
        return idx
    n = BACKFILL_SAMPLE_POINTS
    step = (len(idx) - 1) / (n - 1)
    return sorted({idx[round(i * step)] for i in range(n)})


def update_live_lines(conn: sqlite3.Connection, repo_dir: Path) -> LiveLineResult:
    """Measure per-feature live lines at the tip, refresh features.live_lines,
    and write Sample rows (backfill sweep first when the table is empty)."""
    row = conn.execute("SELECT sha FROM commits ORDER BY walk_index DESC LIMIT 1").fetchone()
    if not row:
        return LiveLineResult("", 0, 0, 0)
    tip = str(row["sha"])
    plan = conn.execute("SELECT walk_index, sha FROM commits ORDER BY walk_index").fetchall()
    walk_of = {str(r["sha"]): int(r["walk_index"]) for r in plan}
    alive = _defining_walks(conn)

    samples_written = 0
    backfilled_points = 0
    have_samples = conn.execute("SELECT COUNT(*) c FROM feature_line_samples").fetchone()["c"]
    if not have_samples and alive:
        by_index = {int(r["walk_index"]): str(r["sha"]) for r in plan}
        for idx in sample_points(sorted(by_index)):
            sha = by_index[idx]
            per_feature, _ = _attribute(conn, *_line_counts_at(repo_dir, sha))
            samples_written += _write_sample(conn, sha, alive, walk_of[sha], per_feature)
            backfilled_points += 1

    per_feature, unattributed = _attribute(conn, *_line_counts_at(repo_dir, tip))
    with conn:
        conn.execute("UPDATE features SET live_lines = 0")
        for fid, n in per_feature.items():
            conn.execute("UPDATE features SET live_lines = ? WHERE id = ?", (n, fid))
    samples_written += _write_sample(conn, tip, alive, walk_of[tip], per_feature)
    conn.commit()
    return LiveLineResult(tip, unattributed, samples_written, backfilled_points)


def _defining_walks(conn: sqlite3.Connection) -> dict[str, int]:
    """feature_id -> walk index at which the feature comes into existence
    (its earliest defining commit; falls back to its earliest link)."""
    out: dict[str, int] = {}
    for r in conn.execute(
        """SELECT cf.feature_id fid, MIN(c.walk_index) w
           FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
           WHERE cf.role = 'defines' GROUP BY cf.feature_id"""
    ):
        out[str(r["fid"])] = int(r["w"])
    for r in conn.execute(
        """SELECT cf.feature_id fid, MIN(c.walk_index) w
           FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
           GROUP BY cf.feature_id"""
    ):
        out.setdefault(str(r["fid"]), int(r["w"]))
    return out


def _write_sample(
    conn: sqlite3.Connection,
    at_sha: str,
    alive: dict[str, int],
    walk_index: int,
    per_feature: dict[str, int],
) -> int:
    """One Sample row per feature alive at this commit (idempotent per sha)."""
    written = 0
    with conn:
        for fid, defined_walk in alive.items():
            if defined_walk > walk_index:
                continue  # the feature does not exist yet at this sample
            conn.execute(
                "INSERT OR REPLACE INTO feature_line_samples"
                " (feature_id, at_commit_sha, live_lines) VALUES (?, ?, ?)",
                (fid, at_sha, per_feature.get(fid, 0)),
            )
            written += 1
    return written


def _line_counts_at(repo_dir: Path, sha: str) -> tuple[Counter[str], int]:
    """(introducing commit -> line count, unblamable lines) at a commit."""
    counts: Counter[str] = Counter()
    unattributed = 0
    for path in _files_at(repo_dir, sha):
        try:
            text = _git(repo_dir, "blame", "-w", "-C", "--porcelain", sha, "--", path)
        except subprocess.CalledProcessError:
            unattributed += _blob_line_count(repo_dir, sha, path)  # binary etc.
            continue
        for blamed in _porcelain_shas(text):
            counts[blamed] += 1
    return counts, unattributed


def _files_at(repo_dir: Path, sha: str) -> list[str]:
    """Tracked paths at a commit, skipping store paths and submodule gitlinks."""
    paths = []
    for line in _git(repo_dir, "ls-tree", "-r", sha).splitlines():
        meta, _, name = line.partition("\t")
        if meta.split()[0] == "160000":
            continue  # gitlinks: blame never descends into submodule content
        if is_store_path(name):
            continue
        paths.append(name)
    return paths


def _blob_line_count(repo_dir: Path, sha: str, path: str) -> int:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"{sha}:{path}"],
        capture_output=True,
        check=False,
    )
    return len(proc.stdout.splitlines()) if proc.returncode == 0 else 0


def _attribute(
    conn: sqlite3.Connection, counts: Counter[str], unblamable: int
) -> tuple[dict[str, int], int]:
    """Commit line counts -> per-feature live lines + unattributed total.
    Unattributed = lines whose introducing commit is not in the DB (plus lines
    blame could not attribute at all)."""
    features_by_commit: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT c.sha, x.feature_id FROM commits c JOIN
               (SELECT commit_id, feature_id FROM commits_features
                UNION SELECT fix_commit_id, feature_id FROM fixes_features) x
               ON x.commit_id = c.id"""
    ):
        features_by_commit.setdefault(str(r["sha"]), set()).add(str(r["feature_id"]))
    known = {str(r["sha"]) for r in conn.execute("SELECT sha FROM commits")}
    per_feature: dict[str, int] = {}
    unattributed = unblamable
    for sha, n in counts.items():
        if sha not in known:
            unattributed += n
            continue
        for fid in features_by_commit.get(sha, ()):
            per_feature[fid] = per_feature.get(fid, 0) + n
    return per_feature, unattributed
