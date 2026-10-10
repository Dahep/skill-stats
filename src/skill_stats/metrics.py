"""Feature-level metrics over SQL + diff scans: features/year, fixes/feature, churn,
and time series (features over time, 1-month-rolling fixes/feature)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple


@dataclass
class FeatureTimePoint:
    month: str  # YYYY-MM
    features_new: int
    features_cumulative: int
    commits: int
    fixes: int


@dataclass
class RollingPoint:
    when: str  # YYYY-MM-DD (window end, UTC-normalized)
    fixes_window: int  # fix commits in the trailing window
    features_touched: int  # distinct features those fixes reach
    fixes_per_feature: float


def _add_months(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _month_fill(first: str, last: str) -> list[str]:
    months = []
    cur = first
    while cur <= last:
        months.append(cur)
        cur = _add_months(cur, 1)
    return months


def features_timeline(conn: sqlite3.Connection) -> list[FeatureTimePoint]:
    """Monthly buckets from first to last walk commit: new/cumulative features,
    commits, and fix commits per bucket."""
    features_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', created_at) m, COUNT(*) n FROM features GROUP BY m"
        )
    }
    commits_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', committed_at) m, COUNT(*) n FROM commits GROUP BY m"
        )
    }
    fixes_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            """SELECT strftime('%Y-%m', c.committed_at) m, COUNT(*) n
               FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = 'fix' GROUP BY m"""
        )
    }
    if not commits_by_month and not features_by_month:
        return []
    first = min(
        min(commits_by_month, default="9999"),
        min(features_by_month, default="9999"),
    )
    last = max(max(commits_by_month, default=""), max(features_by_month, default=""))
    points: list[FeatureTimePoint] = []
    features_seen = 0
    for m in _month_fill(first, last):
        n_new = features_by_month.get(m, 0)
        features_seen += n_new
        points.append(
            FeatureTimePoint(
                month=m,
                features_new=n_new,
                features_cumulative=features_seen,
                commits=commits_by_month.get(m, 0),
                fixes=fixes_by_month.get(m, 0),
            )
        )
    return points


def fixes_rolling(
    conn: sqlite3.Connection, window_days: int = 30, step_days: int = 7
) -> list[RollingPoint]:
    """fixes/feature with a trailing 1-month window, sampled weekly.

    Numerator: all fix commits falling in the trailing window (end -
    window_days + 1 .. window end). Denominator: the distinct features those
    fixes reach through the fixes_features lineage closure — a feature counts
    once however many fixes touch it. Dates are normalized by SQLite
    (strftime) so month buckets and windows share one timezone convention.
    The last sample is always placed at the latest commit date."""
    end_s = conn.execute(
        "SELECT MAX(strftime('%Y-%m-%d', committed_at)) d FROM commits"
    ).fetchone()["d"]
    if not end_s:
        return []
    end = date.fromisoformat(end_s)

    fix_dates: dict[int, date] = {}
    for r in conn.execute(
        """SELECT v.commit_id id, strftime('%Y-%m-%d', c.committed_at) d
           FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
           WHERE v.verdict = 'fix'"""
    ):
        fix_dates[r["id"]] = date.fromisoformat(r["d"])
    if not fix_dates:
        return []

    reached: dict[int, set[str]] = {}
    for r in conn.execute("SELECT fix_commit_id, feature_id FROM fixes_features"):
        reached.setdefault(r["fix_commit_id"], set()).add(r["feature_id"])

    span = timedelta(days=window_days - 1)

    def pt(day: date) -> RollingPoint:
        lo = day - span
        fixes_in = [cid for cid, d in fix_dates.items() if lo <= d <= day]
        touched = set().union(*(reached.get(cid, set()) for cid in fixes_in))
        n = len(fixes_in)
        ratio = round(n / len(touched), 3) if touched else 0.0
        return RollingPoint(day.isoformat(), n, len(touched), ratio)

    points: list[RollingPoint] = []
    cur = min(fix_dates.values())
    while cur <= end:
        points.append(pt(cur))
        cur += timedelta(days=step_days)
    if points[-1].when != end.isoformat():
        points.append(pt(end))
    return points


@dataclass
class MetricSnapshot:
    total_commits: int
    verdict_counts: dict[str, int]
    total_features: int
    total_fixes: int
    years: list[tuple[str, int]]  # year -> features created
    features_per_year: float
    fixes_per_feature: float  # overall mean
    # year -> mean fixes per feature created that year
    fixes_by_year_mean: list[tuple[str, float]]
    top_features_by_fixes: list[tuple[str, str, int]]
    top_features_by_churn: list[tuple[str, str, int]]
    fixes_uncovered: int  # fix verdicts attributed to no feature


def churn_per_feature(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(feature id, title, churn lines) per feature over its linked commits.
    Churn is read from the walk-stored churn_lines columns (derived at walk
    time by gitwalk.prepare_diff — the single source of the counting rules)."""
    rows = conn.execute(
        """SELECT f.id, f.title, COALESCE(SUM(c.churn_lines), 0) n FROM features f
           JOIN commits_features cf ON cf.feature_id = f.id
           JOIN commits c ON c.id = cf.commit_id
           GROUP BY f.id, f.title"""
    ).fetchall()
    out = [(str(r["id"]), str(r["title"]), int(r["n"])) for r in rows]
    return sorted(out, key=lambda kv: (-kv[2], kv[0]))


class FeatureRank(NamedTuple):
    """One feature with its attributed-fix count and total churn (lines)."""

    fid: str
    title: str
    created: str  # YYYY-MM-DD, first defining-commit date
    fixes: int
    churn: int
    sha: str  # earliest defining commit sha


def feature_ranking(conn: sqlite3.Connection) -> list[FeatureRank]:
    """All features ranked by attributed fixes (lineage closure, ties broken
    by feature id), with churn and the earliest defining-commit sha."""
    churn = {fid: n for fid, _t, n in churn_per_feature(conn)}
    sha = {
        r["fid"]: r["sha"]
        for r in conn.execute(
            """SELECT cf.feature_id fid, MIN(c.walk_index) w, c.sha
               FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
               WHERE cf.role = 'defines' GROUP BY cf.feature_id"""
        )
    }
    return [
        FeatureRank(
            fid=str(r["fid"]),
            title=str(r["title"]),
            created=str(r["d"])[:10],
            fixes=int(r["n"]),
            churn=int(churn.get(str(r["fid"]), 0)),
            sha=str(sha.get(r["fid"], "")),
        )
        for r in conn.execute(
            """SELECT f.id fid, f.title, f.created_at d, COUNT(ff.fix_commit_id) n
               FROM features f LEFT JOIN fixes_features ff ON ff.feature_id = f.id
               GROUP BY f.id ORDER BY n DESC, f.id"""
        )
    ]


def snapshot(conn: sqlite3.Connection, top: int = 15) -> MetricSnapshot:
    total_commits = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    verdict_counts = {
        r["verdict"]: r["n"]
        for r in conn.execute("SELECT verdict, COUNT(*) n FROM commit_verdicts GROUP BY verdict")
    }
    total_features = conn.execute("SELECT COUNT(*) c FROM features").fetchone()["c"]
    total_fixes = verdict_counts.get("fix", 0)

    years = [
        (r["yr"], r["n"])
        for r in conn.execute(
            "SELECT strftime('%Y', created_at) yr, COUNT(*) n FROM features GROUP BY yr ORDER BY yr"
        )
    ]
    span_years = max(1, len(years))
    features_per_year = total_features / span_years

    fixes_per_feature_map = {
        r["fid"]: r["n"]
        for r in conn.execute(
            "SELECT feature_id fid, COUNT(DISTINCT fix_commit_id) n"
            " FROM fixes_features GROUP BY feature_id"
        )
    }
    fixes_per_feature = (
        sum(fixes_per_feature_map.values()) / total_features if total_features else 0.0
    )

    fixes_by_year: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT id, title, strftime('%Y', created_at) yr FROM features ORDER BY id"
    ):
        fixes_by_year.setdefault(r["yr"], []).append(fixes_per_feature_map.get(r["id"], 0))
    fixes_by_year_mean = [(yr, sum(vals) / len(vals)) for yr, vals in sorted(fixes_by_year.items())]

    top_features_by_fixes = [(r.fid, r.title, r.fixes) for r in feature_ranking(conn)[:top]]

    fixes_uncovered = conn.execute(
        """SELECT COUNT(*) c FROM commit_verdicts v
           WHERE v.verdict = 'fix'
             AND NOT EXISTS
               (SELECT 1 FROM fixes_features ff WHERE ff.fix_commit_id = v.commit_id)"""
    ).fetchone()["c"]

    churn = churn_per_feature(conn)
    return MetricSnapshot(
        total_commits=total_commits,
        verdict_counts=verdict_counts,
        total_features=total_features,
        total_fixes=total_fixes,
        years=years,
        features_per_year=features_per_year,
        fixes_per_feature=fixes_per_feature,
        fixes_by_year_mean=fixes_by_year_mean,
        top_features_by_fixes=top_features_by_fixes,
        top_features_by_churn=churn[:top],
        fixes_uncovered=fixes_uncovered,
    )


def snapshot_to_json(snap: MetricSnapshot) -> str:
    return json.dumps(
        {
            "total_commits": snap.total_commits,
            "verdicts": snap.verdict_counts,
            "features": snap.total_features,
            "fixes": snap.total_fixes,
            "features_per_year": round(snap.features_per_year, 2),
            "fixes_per_feature": round(snap.fixes_per_feature, 3),
            "features_per_year_byyear": dict(snap.years),
            "fixes_per_feature_byyear": {y: round(v, 2) for y, v in snap.fixes_by_year_mean},
            "top_features_by_fixes": [
                {"id": fid, "title": t, "fixes": n} for fid, t, n in snap.top_features_by_fixes
            ],
            "top_features_by_churn": [
                {"id": fid, "title": t, "churn": n} for fid, t, n in snap.top_features_by_churn
            ],
            "fixes_uncovered": snap.fixes_uncovered,
        },
        indent=2,
    )
