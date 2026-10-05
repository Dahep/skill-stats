"""HTML/CLI report: text summary + matplotlib charts (opt-in via -c/--charts)."""

import sqlite3
from pathlib import Path

from .metrics import snapshot


def text_report(conn: sqlite3.Connection, top: int = 15) -> str:
    snap = snapshot(conn, top=top)
    lines = [
        "=" * 60,
        f"Walk {snap.total_commits} commits on Target branch",
        f"Verdicts: {snap.verdict_counts}",
        f"Features: {snap.total_features}", 
        f"Fixes:    {snap.total_fixes}",
        f"Features / year: {snap.features_per_year:.1f}",
        f"Fixes / feature: {snap.fixes_per_feature:.2f}",
        f"Fixes attributable to no feature: {snap.fixes_uncovered}",
        "",
        "features / year:",
    ]
    for yr, n in snap.years:
        lines.append(f"  {yr}: {n}")
    lines.append("")
    lines.append("fixes / feature by year created:")
    for yr, v in snap.fixes_by_year_mean:
        lines.append(f"  {yr}: {v:.2f}")
    lines.append("")
    lines.append(f"top {top} features by fixes:")
    for fid, title, n in snap.top_features_by_fixes:
        lines.append(f"  #{fid:<4} {title:<48} {n}")
    lines.append("")
    lines.append(f"top {top} features by churn:")
    for fid, title, n in snap.top_features_by_churn:
        lines.append(f"  #{fid:<4} {title:<48} {n}")
    lines.append("=" * 60)
    return "\n".join(lines)


def _charts(conn: sqlite3.Connection, out_dir: str) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    snap = snapshot(conn)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    fig, ax = plt.subplots(figsize=(10, 4))
    yrs = [y for y, _ in snap.years]
    vals = [n for _, n in snap.years]
    ax.bar(yrs, [float(v) for v in vals])
    ax.set_title("features / year")
    ax.set_ylabel("features")
    p = f"{out_dir}/features_per_year.png"
    fig.savefig(p, bbox_inches="tight")
    written.append(p)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4))
    yrs2 = [y for y, _ in snap.fixes_by_year_mean]
    vals2 = [float(v) for _, v in snap.fixes_by_year_mean]
    ax.bar(yrs2, vals2)
    ax.set_title("fixes / feature (by feature creation year)")
    ax.set_ylabel("fixes per feature")
    p = f"{out_dir}/fixes_per_feature_over_time.png"
    fig.savefig(p, bbox_inches="tight")
    written.append(p)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 8))
    top = snap.top_features_by_fixes[:20]
    labels = [f"#{fid} {title[:36]}" for fid, title, _n in top][::-1]
    vals = [int(n) for _, _, n in top][::-1]
    ax.barh(labels, vals)
    ax.set_title("features with most fixes")
    ax.set_xlabel("fixes")
    p = f"{out_dir}/fixes_by_feature_top.png"
    fig.savefig(p, bbox_inches="tight")
    written.append(p)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 8))
    top = snap.top_features_by_churn[:20]
    labels = [f"#{fid} {title[:36]}" for fid, title, _n in top][::-1]
    vals = [int(n) for _, _, n in top][::-1]
    ax.barh(labels, vals)
    ax.set_title("features with most churn")
    ax.set_xlabel("lines changed")
    p = f"{out_dir}/churn_by_feature_top.png"
    fig.savefig(p, bbox_inches="tight")
    written.append(p)
    plt.close(fig)

    return written
