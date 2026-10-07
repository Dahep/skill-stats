"""Feature-level metrics over SQL + diff scans: features/year, fixes/feature, churn,
and time series (features over time, 1-month-rolling fixes/feature)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class FeatureTimePoint:
    month: str                    # YYYY-MM
    features_new: int
    features_cumulative: int
    commits: int
    fixes: int


@dataclass
class RollingPoint:
    when: str                     # YYYY-MM-DD (window end)
    fixes_window: int             # fix commits in the trailing window
    features_existing: int        # features created by window end
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
            "SELECT strftime('%Y-%m', created_at) m, COUNT(*) n FROM features GROUP BY m")
    }
    commits_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', committed_at) m, COUNT(*) n FROM commits GROUP BY m")
    }
    fixes_by_month = {
        r["m"]: r["n"]
        for r in conn.execute(
            """SELECT strftime('%Y-%m', c.committed_at) m, COUNT(*) n
               FROM commits c JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = 'fix' GROUP BY m""")
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

    Numeric fix commits falling in the trailing window (end - window_days + 1
    .. window end), divided by the features created by the window end."""
    fix_dates = sorted(
        date.fromisoformat(r["d"][:10])
        for r in conn.execute(
            """SELECT c.committed_at d FROM commits c
               JOIN commit_verdicts v ON v.commit_id = c.id
               WHERE v.verdict = 'fix'""")
    )
    feature_dates = sorted(
        date.fromisoformat(r["d"][:10])
        for r in conn.execute("SELECT created_at d FROM features")
    )
    end = date.fromisoformat(
        conn.execute("SELECT MAX(committed_at) d FROM commits").fetchone()["d"][:10]
    )
    if not fix_dates:
        return []
    points: list[RollingPoint] = []
    cur = fix_dates[0]
    span = timedelta(days=window_days - 1)
    while cur <= end:
        lo = cur - span
        fixes_window = sum(1 for d in fix_dates if lo <= d <= cur)
        features_existing = sum(1 for d in feature_dates if d <= cur)
        ratio = fixes_window / features_existing if features_existing else 0.0
        points.append(
            RollingPoint(
                when=cur.isoformat(),
                fixes_window=fixes_window,
                features_existing=features_existing,
                fixes_per_feature=round(ratio, 3),
            )
        )
        cur += timedelta(days=step_days)
    return points


@dataclass
class MetricSnapshot:
    total_commits: int
    verdict_counts: dict[str, int]
    total_features: int
    total_fixes: int
    years: list[tuple[str, int]]                      # year -> features created
    features_per_year: float
    fixes_per_feature: float                          # overall mean
    # year -> mean fixes per feature created that year
    fixes_by_year_mean: list[tuple[str, float]]
    top_features_by_fixes: list[tuple[int, str, int]]
    top_features_by_churn: list[tuple[int, str, int]]
    fixes_uncovered: int                              # fix verdicts attributed to no feature


def _diff_churn(diff: str) -> int:
    added = sum(
        1 for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deleted = sum(
        1 for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return added + deleted


def churn_per_feature(conn: sqlite3.Connection) -> list[tuple[int, str, int]]:
    rows = conn.execute(
        """SELECT f.id, f.title, cf.commit_id FROM features f
           JOIN commits_features cf ON cf.feature_id = f.id
           GROUP BY f.id, cf.commit_id"""
    ).fetchall()

    commit_diffs = {
        r["id"]: r["diff"]
        for r in conn.execute("SELECT id, diff FROM commits")
    }
    churn: dict[int, tuple[str, int]] = {}
    for r in rows:
        fid, title = int(r["id"]), str(r["title"])
        lines = int(churn.get(fid, (title, 0))[1]) + _diff_churn(commit_diffs[r["commit_id"]])
        churn[fid] = (title, lines)
    return sorted(
        ((fid, t, n) for fid, (t, n) in churn.items()), key=lambda kv: -kv[2]
    )


def snapshot(conn: sqlite3.Connection, top: int = 15) -> MetricSnapshot:
    total_commits = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    verdict_counts = {
        r["verdict"]: r["n"]
        for r in conn.execute(
            "SELECT verdict, COUNT(*) n FROM commit_verdicts GROUP BY verdict"
        )
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
    fixes_by_year_mean = [
        (yr, sum(vals) / len(vals)) for yr, vals in sorted(fixes_by_year.items())
    ]

    top_features_by_fixes = [
        (r["id"], r["title"], r["n"])
        for r in conn.execute(
            """SELECT f.id, f.title, COUNT(ff.fix_commit_id) n FROM features f
                LEFT JOIN fixes_features ff ON ff.feature_id = f.id
                GROUP BY f.id ORDER BY n DESC LIMIT ?""",
            (top,),
        )
    ]

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
