"""CLI: uv run skill-stats {init,walk,classify,detect,lineage,report,update}. Requires the
opencode CLI on PATH for classify (ADR-0001 rolling session). update is the
orchestrating verb (ADR-0003 clause 7); the stage subcommands stay plumbing."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import db, gitwalk, identity
from .classify import DEFAULT_MODEL, SESSION_DIR, Session, classify_pending
from .lineage import close_lineage
from .targets import annotate_all


def _settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")
    }


def _set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()


def new_db(default: str, args: argparse.Namespace) -> Path:
    p = Path(getattr(args, "db", None) or default)
    p.parent.mkdir(parents=True, exist_ok=True)
    db.connect(p)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="skill-stats")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create a DB and store run config")
    init.add_argument("repo")
    init.add_argument("--branch", default=None)
    init.add_argument("--model", default=DEFAULT_MODEL)
    init.add_argument("--min-target-lines", type=int, default=1)
    init.add_argument("--db", default="skill-stats.db")

    walk = sub.add_parser("walk", help="walk Target branch into DB")
    walk.add_argument("--db", default="skill-stats.db")

    cls = sub.add_parser("classify", help="LLM roll walk (opencode CLI)")
    cls.add_argument("--db", default="skill-stats.db")
    cls.add_argument("--limit", type=int, default=None)
    cls.add_argument("--fresh", action="store_true", help="drop saved opencode session")

    det = sub.add_parser("detect", help="blame evidence -> fix_touches/fix_targets")
    det.add_argument("--db", default="skill-stats.db")

    lin = sub.add_parser("lineage", help="fixes_features transitive closure")
    lin.add_argument("--db", default="skill-stats.db")

    rep = sub.add_parser("report", help="metrics text summary + optional charts")
    rep.add_argument("--db", default="skill-stats.db")
    rep.add_argument("--charts", action="store_true")
    rep.add_argument("--html", nargs="?", const="skill-stats-report.html", default=None)
    rep.add_argument("--out", default="skill-stats-report")
    rep.add_argument("--json", action="store_true")

    up = sub.add_parser(
        "update",
        help="verify -> walk -> classify -> detect -> lineage -> live-lines ->"
        " report -> serialize + digest",
    )
    up.add_argument("--db", default="skill-stats.db")
    up.add_argument("--no-classify", action="store_true", help="skip LLM classification")
    up.add_argument("--no-detect", action="store_true", help="skip blame-based fix detection")
    up.add_argument("--no-lineage", action="store_true", help="skip lineage closure")
    up.add_argument("--no-report", action="store_true", help="skip report.html regeneration")
    up.add_argument("--no-serialize", action="store_true", help="skip artifact write")

    args = parser.parse_args(argv)

    settings: dict[str, Any] = {}
    if args.cmd == "init":
        repo = Path(args.repo).resolve()
        if not (repo / ".git").exists():
            sys.exit(f"not a git repo: {repo}")
        db_path = new_db(args.db, args)
        conn = db.connect(db_path)
        branch = args.branch or gitwalk.current_branch(repo)
        identity.register_repository(conn, repo, branch)  # writes repositories row + repo_id
        cfg = {
            "repo": str(repo),
            "branch": branch,
            "model": args.model,
            "min_target_lines": args.min_target_lines,
        }
        for key, value in cfg.items():
            _set(conn, key, value)
        conn.close()
        print(f"initialized {db_path} for {repo} (branch {branch}, model {args.model})")
        return

    conn = db.connect(Path(args.db))
    settings = _settings(conn)
    repo_dir: Path | None = Path(settings["repo"]) if "repo" in settings else None

    if args.cmd == "walk":
        assert repo_dir is not None, "run init first"
        n = gitwalk.walk(repo_dir, conn, settings.get("branch") or None)
        total = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
        print(f"walked {n} new commits (total {total})")
    elif args.cmd == "classify":
        assert repo_dir is not None, "run init first"
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        sid = None if args.fresh else (settings.get("classify_session_id") or None)
        session = Session(settings.get("model", DEFAULT_MODEL), sid)
        try:
            done = classify_pending(
                repo_dir, conn, settings.get("model", DEFAULT_MODEL), session, limit=args.limit
            )
        finally:
            if session.session_id:
                _set(conn, "classify_session_id", session.session_id)
        print(f"classified {len(done)} commits (session {session.session_id})")
    elif args.cmd == "detect":
        assert repo_dir is not None, "run init first"
        n_fixes = annotate_all(repo_dir, conn, int(settings.get("min_target_lines", 1)))
        print(f"annotated {n_fixes} fix commits")
    elif args.cmd == "lineage":
        n_rows = close_lineage(conn)
        rows_now = conn.execute("SELECT COUNT(*) c FROM fixes_features").fetchone()["c"]
        print(f"closure rows now {rows_now} (+{n_rows})")
    elif args.cmd == "report":
        from .report import text_report

        out = text_report(conn)
        if args.json:
            from .metrics import snapshot as _snap
            from .metrics import snapshot_to_json

            print(snapshot_to_json(_snap(conn)))
        else:
            print(out)
        if args.charts:
            from .report import _charts

            paths = _charts(conn, args.out)
            for p in paths:
                print(f"wrote {p}")
        if getattr(args, "html", None):
            from .htmlreport import write_report

            html_path = write_report(conn, args.html)
            print(f"wrote {html_path}")
    elif args.cmd == "update":
        assert repo_dir is not None, "run init first"
        _run_update(conn, repo_dir, settings, args)
    conn.close()


def _run_update(
    conn: sqlite3.Connection, repo_dir: Path, settings: dict[str, Any], args: argparse.Namespace
) -> None:
    """One orchestrating run (ADR-0003 clause 7, Q11): verify the existing
    artifact first, then walk -> classify -> detect -> lineage -> live-lines ->
    report -> serialize + digest + verify. Deterministic stages always run;
    classification is best-effort — when it cannot run the walked backlog stays
    pending, is printed loudly, and reprocesses on a later successful update
    (Q13). The exit code stays 0 with a backlog."""
    from . import artifact, livelines
    from .htmlreport import write_report

    branch = settings.get("branch") or None
    store = repo_dir / ".skill-stats"
    art_path = store / "skill-stats.sql"

    if art_path.exists():
        err = artifact.verify(art_path)
        # wave 3 adds the partial-recompute recovery; for now the verdict is
        # loud and the rewrite below supersedes the (untrusted) file
        print(f"artifact digest: {'ok' if err is None else 'MISMATCH (' + err + ')'}")
    else:
        print("artifact: none yet (first run)")

    n = gitwalk.walk(repo_dir, conn, branch)
    print(f"walked {n} new commits")

    if not args.no_classify:
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            sid = settings.get("classify_session_id") or None
            session = Session(settings.get("model", DEFAULT_MODEL), sid)
            try:
                done = classify_pending(
                    repo_dir, conn, settings.get("model", DEFAULT_MODEL), session
                )
            finally:
                if session.session_id:
                    _set(conn, "classify_session_id", session.session_id)
            print(f"classified {len(done)} commits")
        except (OSError, RuntimeError) as exc:
            print(f"WARNING: classification unavailable ({exc}); backlog stays pending")

    if not args.no_detect:
        n_fixes = annotate_all(repo_dir, conn, int(settings.get("min_target_lines", 1)))
        print(f"annotated {n_fixes} fix commits")
    if not args.no_lineage:
        close_lineage(conn)
        print("lineage closure updated")

    live = livelines.update_live_lines(conn, repo_dir)
    print(
        f"live lines at {live.at_sha[:12] or '(none)'}: unattributed"
        f" {live.unattributed_lines}, {live.samples_written} samples written"
    )

    if not args.no_report:
        report_path = write_report(conn, store / "report.html")
        print(f"wrote {report_path}")
    if not args.no_serialize:
        path = artifact.write(conn, repo_dir, with_report=False)
        err = artifact.verify(path)
        print(f"wrote {path} (digest {'ok' if err is None else 'MISMATCH ' + str(err)})")

    total = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    classified = conn.execute("SELECT COUNT(*) c FROM commit_verdicts").fetchone()["c"]
    pending = int(total) - int(classified)
    suffix = "artifact updated" if not args.no_serialize else "artifact not written"
    print(f"{total} walked, {classified} classified, {pending} pending, {suffix}")
    if pending:
        print(
            f"WARNING: {pending} commits unclassified — backlog; a later successful"
            " update reprocesses them"
        )


if __name__ == "__main__":
    main()
