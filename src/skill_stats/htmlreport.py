"""Self-contained HTML report: inline SVG charts, inline CSS, no external assets.

Renders one page from the pipeline DB: fix ranking per feature, features over
time, 1-month-rolling fixes/feature, plus the rest of the gathered snapshot
(verdict mix, commits/month, churn, data quality, run metadata).
"""

import html
import json
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from .metrics import churn_per_feature, features_timeline, fixes_rolling, snapshot

C = {
    "bg": "#0f1117",
    "card": "#161a26",
    "border": "#232a3d",
    "text": "#c0caf5",
    "muted": "#7d88b0",
    "grid": "#28314a",
    "feature": "#9ece6a",
    "cumulative": "#bb9af7",
    "fix": "#f7768e",
    "ratio": "#7aa2f7",
    "churn": "#e0af68",
    "neutral": "#73daca",
}

CSS = f"""
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0 auto; max-width: 1020px; padding: 24px 18px 64px;
  background: {C["bg"]}; color: {C["text"]};
  font: 14px/1.55 "Inter", "Segoe UI", system-ui, sans-serif;
}}
h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.02em; }}
h2 {{ font-size: 16px; margin: 34px 0 10px; letter-spacing: -0.01em; }}
a {{ color: {C["ratio"]}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.muted {{ color: {C["muted"]}; font-size: 12.5px; }}
.kpis {{ display: grid; grid-template-columns: repeat(3, 1fr);
        gap: 12px; margin: 22px 0 4px; }}
.kpi {{ background: {C["card"]}; border: 1px solid {C["border"]};
       border-radius: 10px; padding: 14px 16px; }}
.kpi-value {{ font-size: 26px; font-weight: 650; letter-spacing: -0.02em; }}
.kpi-sub {{ color: {C["muted"]}; font-size: 11.5px; }}
.kpi-label {{ color: {C["muted"]}; font-size: 12px; margin-top: 2px; }}
.note {{ color: {C["muted"]}; font-size: 12px; margin: 6px 0 12px; }}
.legend {{ display: flex; gap: 18px; font-size: 12px; color: {C["muted"]};
          margin: 8px 2px 0; align-items: center; }}
.chartbox {{ margin-top: 6px; }}
.donut-row {{ display: flex; gap: 22px; align-items: center;
             background: {C["card"]}; border: 1px solid {C["border"]};
             border-radius: 10px; padding: 16px 20px; flex-wrap: wrap; }}
.badge {{ display: flex; align-items: center; gap: 7px; margin: 3px 0;
         font-size: 13px; }}
.badge b {{ font-variant-numeric: tabular-nums; }}
.dot {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block;
       flex: none; }}
.tick {{ font: 10.5px "Inter", sans-serif; fill: {C["muted"]}; }}
.donut-total {{ font: 22px "Inter", sans-serif; fill: {C["text"]};
               font-weight: 650; }}
table {{ border-collapse: collapse; width: 100%;
        background: {C["card"]}; border: 1px solid {C["border"]};
        border-radius: 10px; overflow: hidden; font-size: 13px; }}
th, td {{ padding: 7px 12px; text-align: left;
         border-bottom: 1px solid {C["border"]}; }}
th {{ color: {C["muted"]}; font-weight: 550; font-size: 12px;
     background: {C["bg"]}; }}
tbody tr:last-child td {{ border-bottom: none; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.foot {{ margin-top: 40px; color: {C["muted"]}; font-size: 12px;
        border-top: 1px solid {C["border"]}; padding-top: 12px; }}
.foot code {{ color: {C["text"]}; }}
@media (max-width: 720px) {{ .kpis {{ grid-template-columns: 1fr 1fr; }} }}
@media print {{
  @page {{ size: A4; margin: 9mm; }}
  body {{ max-width: none; padding: 0; margin: 0; }}
  h2 {{ margin: 16px 0 6px; }}
  section {{ margin: 0; }}
  .kpi {{ padding: 10px 14px; }}
  .kpi-value {{ font-size: 21px; }}
  .kpis {{ gap: 8px; margin: 14px 0 2px; }}
  .note {{ margin: 4px 0 6px; }}
  .chartbox {{ margin-top: 2px; }}
  .legend {{ margin: 4px 2px 0; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  .kpi, .donut-row, svg {{ break-inside: avoid; }}
  .chartbox {{ break-inside: avoid; }}
  tr {{ break-inside: avoid; }}
  thead {{ display: table-header-group; }}
  a {{ color: inherit; }}
}}
"""


def _esc(s: str) -> str:
    return html.escape(str(s))


def _repo_url(repo: str | None) -> str | None:
    """https github URL from the repo's origin (ssh github aliases map to
    github.com); None when the repo is absent or not on github."""
    if not repo:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", repo, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if out.startswith("git@") and "@" in out:
        host, _, path = out[4:].partition(":")
        if "github" in host and path.endswith(".git"):
            return f"https://github.com/{path[:-4]}"
    if out.startswith("https://") and "github.com" in out:
        return out.removesuffix(".git")
    return None


# ---------------------------------------------------------------- data ----

Row = tuple[int, str, str, int, int, str]


def feature_rows(conn: sqlite3.Connection, top: int | None = None) -> list[Row]:
    """All features ranked by attributed fixes (lineage closure), with churn
    lines and the earliest defining-commit sha."""
    churn = {fid: n for fid, _t, n in churn_per_feature(conn)}
    sha = {
        r["fid"]: r["sha"]
        for r in conn.execute(
            """SELECT cf.feature_id fid, MIN(c.walk_index) w, c.sha
               FROM commits_features cf JOIN commits c ON c.id = cf.commit_id
               WHERE cf.role = 'defines' GROUP BY cf.feature_id"""
        )
    }
    rows = [
        (
            int(r["fid"]), str(r["title"]), str(r["d"])[:10],
            int(r["n"]), int(churn.get(r["fid"], 0)), str(sha.get(r["fid"], "")),
        )
        for r in conn.execute(
            """SELECT f.id fid, f.title, f.created_at d, COUNT(ff.fix_commit_id) n
               FROM features f LEFT JOIN fixes_features ff ON ff.feature_id = f.id
               GROUP BY f.id ORDER BY n DESC, f.id"""
        )
    ]
    return rows[:top] if top else rows


# ------------------------------------------------------------- svg chars --

def _dual_chart(
    bars: list[float], labels: list[str], color: str,
    line: list[float] | None = None, line_color: str = C["ratio"],
    bar_name: str = "", line_name: str = "",
    width: int = 920, height: int = 300,
) -> str:
    """Vertical bars with an optional line on its own right-hand scale."""
    pad_l, pad_r, pad_t, pad_b = 56, (76 if line else 18), 24, 40
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = max(1, len(bars))
    ymax_b = max(bars or [0.0]) * 1.08 or 1.0
    ymax_l = max(line or [0.0]) * 1.08 or 1.0

    body = [f'<rect width="{width}" height="{height}" fill="{C["card"]}" rx="8"/>']
    for k in range(5):  # gridlines + left axis labels per bar scale
        frac = k / 4
        y = pad_t + ph * (1 - frac)
        body.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"'
            f' stroke="{C["grid"]}" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" class="tick"'
            f' text-anchor="end">{ymax_b * frac:.4g}</text>'
        )
        if line:
            body.append(
                f'<text x="{width - pad_r + 8}" y="{y + 3.5:.1f}" class="tick"'
                f' fill="{line_color}">{ymax_l * frac:.4g}</text>'
            )

    def sx(i: int) -> float:
        return pad_l + (i + 0.5) * pw / n

    for i, v in enumerate(bars):  # bars
        if v <= 0:
            continue
        h = ph * v / ymax_b
        body.append(
            f'<rect x="{sx(i) - pw / n * 0.32:.1f}" y="{pad_t + ph - h:.1f}"'
            f' width="{pw / n * 0.64:.1f}" height="{h:.1f}" rx="3" fill="{color}"'
            f' opacity="0.9"><title>{_esc(labels[i])}: {v:.4g} {bar_name}</title></rect>'
        )
    if line:  # polyline + dots on its own scale
        pts = " ".join(
            f"{sx(i):.1f},{pad_t + ph * (1 - v / ymax_l):.1f}" for i, v in enumerate(line)
        )
        body.append(
            f'<polyline points="{pts}" fill="none" stroke="{line_color}" stroke-width="2"/>'
        )
        body.extend(
            f'<circle cx="{sx(i):.1f}" cy="{pad_t + ph * (1 - v / ymax_l):.1f}" r="2.6"'
            f' fill="{line_color}"><title>{_esc(labels[i])}: {v:.4g}'
            f" {line_name}</title></circle>"
            for i, v in enumerate(line)
        )
    step = max(1, n // 11)
    for i in range(0, n, step):  # x tick labels
        body.append(
            f'<text x="{sx(i):.1f}" y="{height - 16}" class="tick"'
            f' text-anchor="middle">{_esc(labels[i])}</text>'
        )
    legend = ""
    if bar_name or line_name:
        pieces = []
        if bar_name:
            pieces.append(
                f'<span style="display:inline-block;width:10px;height:10px;'
                f'border-radius:2px;background:{color}"></span>'
                f" {_esc(bar_name)}"
            )
        if line_name:
            pieces.append(
                f'<span style="display:inline-block;width:12px;height:2px;'
                f'background:{line_color}"></span>'
                f" {_esc(line_name)} (right scale)"
            )
        legend = f'<div class="legend">{"".join(pieces)}</div>'
    return (
        f'<div class="chartbox">{legend}'
        + f'<svg viewBox="0 0 {width} {height}" role="img" style="width:100%">'
        + "".join(body) + "</svg></div>"
    )


def _hbars(
    pairs: list[tuple[str, float]], color: str, unit: str, cutoff: int = 40,
) -> str:
    w, pad_l, pad_t, row_h = 920, 300, 18, 27
    height = pad_t + len(pairs) * row_h + 12
    mval = max((v for _, v in pairs), default=1.0) or 1.0
    rows = []
    for i, (label, v) in enumerate(pairs):
        y = pad_t + i * row_h
        bw = (w - pad_l - 64) * float(v) / mval
        short = label if len(label) <= cutoff else label[: cutoff - 1] + "…"
        rows.append(
            f'<text x="8" y="{y + 14:.0f}" class="tick" text-anchor="start">'
            f"<tspan><title>{_esc(label)}</title>{_esc(short)}</tspan></text>"
            f'<rect x="{pad_l}" y="{y + 2:.0f}" width="{bw:.1f}"'
            f' height="{row_h - 10:.0f}" rx="4" fill="{color}" opacity="0.85">'
            f"<title>{_esc(label)}: {v:.4g} {unit}</title></rect>"
            f'<text x="{pad_l + bw + 8:.1f}" y="{y + 14:.0f}" class="tick">{v:.4g}</text>'
        )
    return (
        f'<svg viewBox="0 0 {w} {height}" style="width:100%">'
        f'<rect width="{w}" height="{height}" fill="{C["card"]}" rx="8"/>'
        f'{"".join(rows)}</svg>'
    )


def _donut(counts: list[tuple[str, int, str]]) -> str:
    total = sum(n for _, n, _ in counts) or 1
    r, cx, cy = 62, 90, 90
    circ = 2 * 3.1415926 * r
    segs: list[str] = []
    offset = circ * 0.25
    for name, n, color in counts:
        if n <= 0:
            continue
        frac = n / total
        segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}"'
            f' stroke-width="15" stroke-dasharray="{frac * circ:.2f} {circ:.2f}"'
            f' stroke-dashoffset="{offset:.2f}" transform="rotate(-90 {cx} {cy})">'
            f"<title>{_esc(name)}: {n}</title></circle>"
        )
        offset -= frac * circ
    center = (
        f'<text x="{cx}" y="{cy - 3}" text-anchor="middle" class="donut-total">{total}</text>'
        f'<text x="{cx}" y="{cy + 15}" text-anchor="middle" class="tick">commits'
        "</text>"
    )
    legend = "".join(
        f'<div class="badge"><span class="dot" style="background:{color}"></span>'
        f"{_esc(name)} &nbsp;<b>{n}</b></div>"
        for name, n, color in counts
        if n > 0
    )
    return (
        f'<div class="donut-row"><svg viewBox="0 0 180 180" style="width:168px;'
        f'height:168px">{"".join(segs)}{center}</svg><div>{legend}</div></div>'
    )


# --------------------------------------------------------------- layout ---

def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="kpi"><div class="kpi-value">{_esc(value)}</div>'
        f'<div class="kpi-label">{_esc(label)}</div>{sub_html}</div>'
    )


def build(conn: sqlite3.Connection, top: int = 15) -> str:
    snap = snapshot(conn, top=top)
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings: dict[str, str] = {}
    for r in rows:  # values are stored JSON-encoded (cli._set); parse, else raw
        try:
            settings[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            settings[r["key"]] = r["value"]
    repo = str(settings.get("repo", ""))
    model = str(settings.get("model", ""))
    min_lines = int(settings.get("min_target_lines", 1))
    url = _repo_url(repo)
    repo_name = repo.rstrip("/").split("/")[-1] or repo
    branch = str(settings.get("branch", "main"))

    tl = features_timeline(conn)
    months = [p.month for p in tl]
    rolling = fixes_rolling(conn)
    roll_labels = [p.when for p in rolling]

    first = conn.execute("SELECT MIN(committed_at) d FROM commits").fetchone()["d"] or ""
    last = conn.execute("SELECT MAX(committed_at) d FROM commits").fetchone()["d"] or ""
    span = f"{first[:10]} → {last[:10]}" if first else "—"
    ranking = feature_rows(conn)
    hbars_fixes = _hbars(
        [(f"#{fid} {t}", n) for fid, t, _d, n, _c, _s in ranking[:top]],
        C["fix"], "fixes",
    )

    kpis = (
        _kpi("commits walked", str(snap.total_commits), sub=f"span {span}")
        + _kpi("features", str(snap.total_features),
               sub=f"{snap.features_per_year:.1f} / year")
        + _kpi("fixes", str(snap.total_fixes),
               sub=f"{snap.fixes_per_feature:.2f} per feature")
        + _kpi("top feature fixes", str(snap.top_features_by_fixes[0][2])
               if snap.top_features_by_fixes else "0",
               sub=snap.top_features_by_fixes[0][1] if snap.top_features_by_fixes else "")
        + _kpi("peak rolling fixes/features",
               f"{max((p.fixes_per_feature for p in rolling), default=0.0):.2f}",
               sub="1-month trailing window")
        + _kpi("fixes w/o target", str(snap.fixes_uncovered),
               sub="fix commits attributed to no feature")
    )

    def percent(x: int) -> float:
        return 100.0 * x / snap.total_commits if snap.total_commits else 0.0

    # ---------------------------------------------------------- pieces ----
    features_growth = _dual_chart(
        [float(p.features_new) for p in tl], months, C["feature"],
        line=[float(p.features_cumulative) for p in tl], line_color=C["cumulative"],
        bar_name="new features / month", line_name="features in total",
    )
    fixes_roller = _dual_chart(
        [float(p.fixes_window) for p in rolling], roll_labels, C["fix"],
        line=[p.fixes_per_feature for p in rolling], line_color=C["ratio"],
        bar_name="fixes in trailing 30d", line_name="fixes / feature",
    )
    commits_month = _dual_chart(
        [float(p.commits) for p in tl], months, C["neutral"],
        line=[float(p.fixes) for p in tl], line_color=C["fix"],
        bar_name="commits / month", line_name="fixes / month",
    )
    verdict_counts = [
        (v, snap.verdict_counts.get(v, 0), col)
        for v, col in (
            ("feature", C["feature"]), ("fix", C["fix"]), ("refactor", C["neutral"]),
            ("revert", C["churn"]), ("cleanup", C["cumulative"]), ("unknown", C["muted"]),
        )
    ]
    donut = _donut(verdict_counts)

    rank_rows = []
    for i, (fid, title, created, fixes, churn, sha) in enumerate(ranking, 1):
        link = (
            f'<a href="{url}/commit/{sha}">{sha[:7]}</a>'
            if url and sha
            else (sha[:7] if sha else "—")
        )
        rank_rows.append(
            f"<tr><td class='num'>{i}</td><td>#{fid} &nbsp;{_esc(title)}</td>"
            f"<td class='num'>{created}</td><td class='num'>{fixes}</td>"
            f"<td class='num'>{churn}</td><td>{link}</td></tr>"
        )
    rank_table = (
        "<table><thead><tr>"
        "<th class='num'>#</th><th>Feature</th><th class='num'>Created</th>"
        "<th class='num'>Fixes</th><th class='num'>Churn (lines)</th>"
        "<th>First commit</th></tr></thead><tbody>"
        + "".join(rank_rows) + "</tbody></table>"
    )

    churn_rows = [
        f"<tr><td class='num'>{i}</td><td>#{fid} {_esc(title)}</td>"
        f"<td class='num'>{n}</td></tr>"
        for i, (fid, title, n) in enumerate(snap.top_features_by_churn, 1)
    ]
    churn_table = (
        "<table><thead><tr><th class='num'>#</th><th>Feature</th>"
        "<th class='num'>Churn (lines)</th></tr></thead>"
        "<tbody>" + "".join(churn_rows) + "</tbody></table>"
    )

    who = f"{repo_name} <span class='muted'>({branch})</span> — {model}"
    other_pct = percent(
        snap.verdict_counts.get("revert", 0)
        + snap.verdict_counts.get("cleanup", 0)
        + snap.verdict_counts.get("refactor", 0)
    )
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    title = f"skill-stats report · {repo_name}"
    html_page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title><style>{CSS}</style></head>
<body>
<h1>skill-stats report — {_esc(repo_name)} <span class="muted">({branch})</span></h1>
<div class="muted">Target repo: {_esc(repo)} &nbsp;·&nbsp; generated {date.today().isoformat()}
 &nbsp;·&nbsp; verdict model: {_esc(model)}</div>

<div class="kpis">{kpis}</div>

<section>
<h2>Features over time</h2>
<div class="note">Features per month created (bars, left axis) and cumulative
features in existence (line, right axis). A feature is an LLM-classified
coherent unit of change; features can span several commits or appear across
multiple features from one commit.</div>
{features_growth}
</section>

<section>
<h2>fixes / feature — trailing 1-month window</h2>
<div class="note">Each point: fix commits (attributed via lineage to any feature)
that fall in the trailing 30 days, divided by the number of features that
existed at the window's end. Sampled every 7 days.</div>
{fixes_roller}
</section>

<section>
<h2>Ranking — features with most fixes</h2>
<div class="note">Fixes reach a feature through the lineage closure
(fix-of-a-fix counts towards the original feature); each fix is counted once
per feature it reaches. The table covers all {len(ranking)} features.</div>
{hbars_fixes}
{rank_table}
</section>

<section>
<h2>Other gathered data</h2>

<h2 style="margin-top:18px;font-size:14px">Commit verdict mix</h2>
{donut}
<div class="note">Every Target-branch commit got one verdict (feature · fix ·
refactor · revert · cleanup); the single verdict drives all feature metrics.
{percent(snap.verdict_counts.get("feature", 0)):.0f}% of commits define or
extend the feature set, {percent(snap.verdict_counts.get("fix", 0)):.0f}% are
fixes, {other_pct}%
refactor/revert/cleanup (non-feature work).</div>

<h2 style="margin-top:18px;font-size:14px">Commit volume</h2>
{commits_month}

<h2 style="margin-top:18px;font-size:14px">Top features by churn (lines changed)</h2>
{churn_table}

<div class="foot">
<strong>Methodology.</strong> Walk: first-parent walk of the Target branch
(<code>{_esc(branch)}</code>); squash-merge work appears once. Verdicts: rolling
LLM session via the opencode CLI (<code>{_esc(model)}</code>), one JSON verdict
per commit. Fixes: blame evidence at the first parent
(<code>git blame -w -C --first-parent</code>, ≥{min_lines} lines). Lineage:
<code>fixes_features</code> transitive closure — a fix of a fix is attributed to
every feature in its chain. Running 1-month window: fix commits ÷
existing features. Generated by <code>skill-stats</code> from
<code>{_esc(db_path)}</code>.
<div class="muted">{who}</div>
</div>
</body></html>"""
    return html_page


def write_report(conn: sqlite3.Connection, out: str | Path) -> Path:
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build(conn), encoding="utf-8")
    return p
