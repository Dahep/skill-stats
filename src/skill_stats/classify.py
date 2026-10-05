"""Commit verdict classification via a rolling opencode CLI session.

Per ADR-0001: walk commits chronologically, ask "new Feature or existing one?",
persist the Feature registry and the CLI session id in settings so runs are
resumable and only new commits get classified. Exactly one verdict per commit
drives metrics; blame evidence is a hint only.
"""

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "opencode/glm-5.3-flash"
SESSION_DIR = Path("/tmp/opencode/skill-stats-classifier")
DIFF_CAP = 12000
BODY_CAP = 2500

PROMPT_HEADER = """You are a git-history analyst. The user feeds you one commit per message. \
Decide, Impartially, a single verdict per commit. Do not read files, do not run \
commands, do not call tools: reply with ONE JSON object on a single line, nothing else.
Reply formats (choose exactly one):
{"k":"new","title":"<2-6 word Feature name>","about":"<one sentence>","why":"<short reason>"}
{"k":"existing","f":<id>,"why":"<short reason>"}
{"k":"fix","why":"<what line-belonging context it repairs>"}
{"k":"refactor","why":"<restructures Feature lines, no defect repair intent>"}
{"k":"revert","why":"<undoes an earlier commit>"}
{"k":"cleanup","why":"<formatting/whitespace/non-semantic>"}
Rules: A commit that introduces coherent new functionality is "new" (defines a Feature). \
A commit that continues/reworks an existing Feature's scope without being a repair is \
"existing" and cites f=id from FEATURES. A commit that repairs or corrects lines of \
earlier work is "fix" (even when the message dresses it up). Whitespace/formatting-only \
is "cleanup". Structure-only restructuring is "refactor". Undoing is "revert". \
When unsure between fix and existing, prefer fix only if the intent is repair."""


@dataclass
class Reply:
    kind: str
    payload: dict[str, str | int]


class Session:
    """One rolling opencode CLI conversation. Responds one text message per turn."""

    def __init__(self, model: str, session_id: str | None = None) -> None:
        self.model = model
        self.session_id = session_id
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, message: str) -> str:
        argv = ["opencode", "run", "--format", "json", "-m", self.model]
        if self.session_id:
            argv += ["-s", self.session_id]
        argv.append(message)
        proc = subprocess.run(
            argv, cwd=SESSION_DIR, capture_output=True, text=True, check=False,
            env={**os.environ, "TERM": "dumb"},
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"opencode CLI failed: {proc.stderr[-2000:]}")
        involved_lines = [
            json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")
        ]
        texts = [
            e["part"].get("text", "")
            for e in involved_lines
            if e.get("type") == "text"
        ]
        if not texts:
            raise RuntimeError(f"opencode CLI produced no text: {involved_lines[-3:]}")
        if not self.session_id:
            sid = involved_lines[-1].get("sessionID")
            if sid:
                self.session_id = sid
            else:
                raise RuntimeError("opencode CLI did not give sessionID")
        return "\n".join(texts)


def parse_reply(text: str) -> Reply:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    line = candidate.splitlines()[0]
    data = json.loads(line.strip("` \t"))
    if data.get("k") not in {"new", "existing", "fix", "refactor", "revert", "cleanup"}:
        raise ValueError(f"unknown k={data.get('k')!r}")
    return Reply(kind=data["k"], payload=data)


def feature_registry(conn: sqlite3.Connection, feature_cap: int = 180) -> str:
    """Compacts the current Feature list for the rolling context. The compact block
    is embedded in each prompt so the model always cites real ids."""
    rows = conn.execute(
        """SELECT f.id, f.title, f.about,
                  (SELECT COUNT(*) FROM commits_features cf WHERE cf.feature_id = f.id) nc,
                  (SELECT c2.sha FROM commits_features cf2
                   JOIN commits c2 ON c2.id = cf2.commit_id
                   WHERE cf2.feature_id = f.id ORDER BY c2.walk_index DESC LIMIT 1) last_sha
           FROM features f ORDER BY f.id"""
    ).fetchall()
    rows = rows[-feature_cap:]
    if not rows:
        return "(none yet)"
    return "\n".join(
        f"{r['id']}: {r['title']} — {r['about']} [commits={r['nc']}, sha={r['last_sha']}]"
        for r in rows
    )


def commit_prompt(conn: sqlite3.Connection, walk_index: int) -> str:
    commit = conn.execute(
        "SELECT * FROM commits WHERE walk_index = ?", (walk_index,)
    ).fetchone()
    diff = commit["diff"] or ""
    if len(diff) > DIFF_CAP:
        diff = diff[:DIFF_CAP] + f"\n... [diff truncated, {len(commit['diff'])} chars total]"
    body = commit["message"] or ""
    if len(body) > BODY_CAP:
        body = body[:BODY_CAP] + " ... [body truncated]"
    merged_note = " (merge commit)" if commit["kind"] == "merge" else ""
    return PROMPT_HEADER + f"""

COMMIT {commit['sha'][:12]} walk={walk_index} {commit['committed_at']}{merged_note}
TITLE: {commit['title']}
BODY:
{body}
DIFF:
{diff}

FEATURES (id: title — about [commits, sha]):
{feature_registry(conn)}

Classify COMMIT {commit['sha'][:12]}: one JSON object line."""


K_TO_VERDICT = {
    "new": "feature",
    "existing": "feature",
    "fix": "fix",
    "refactor": "refactor",
    "revert": "revert",
    "cleanup": "cleanup",
}


def apply_reply(conn: sqlite3.Connection, commit_id: int, committed_at: str,
                reply: Reply, raw: str, model: str) -> str:
    """Store verdict + feature rows atomically. Returns the verdict."""
    verdict = K_TO_VERDICT[reply.kind]
    rationale = " ".join(str(reply.payload.get("why", ""))[:300].split()) or None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO commit_verdicts "
            "(commit_id, verdict, rationale, raw_llm_output, model, classified_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (commit_id, verdict, rationale or "", raw, model),
        )
        if reply.kind == "new":
            title = str(reply.payload.get("title") or commit_at(conn, commit_id)["title"]).strip()
            about = str(reply.payload.get("about") or "").strip()
            cur = conn.execute(
                "INSERT INTO features (title, about, created_at) VALUES (?, ?, ?)",
                (title, about, committed_at),
            )
            conn.execute(
                "INSERT INTO commits_features (commit_id, feature_id, role)"
                " VALUES (?, ?, 'defines')",
                (commit_id, cur.lastrowid),
            )
        elif reply.kind == "existing":
            feature_id = int(reply.payload["f"])
            if not conn.execute(
                "SELECT 1 FROM features WHERE id = ?", (feature_id,)
            ).fetchone():
                raise ValueError(f"cited unknown feature id {feature_id}")
            conn.execute(
                "INSERT OR IGNORE INTO commits_features (commit_id, feature_id, role)"
                " VALUES (?, ?, 'touches')",
                (commit_id, feature_id),
            )
    return verdict


def commit_at(conn: sqlite3.Connection, commit_id: int) -> sqlite3.Row:
    out: sqlite3.Row | None = conn.execute(
        "SELECT title, committed_at FROM commits WHERE id = ?", (commit_id,)
    ).fetchone()
    assert out is not None
    return out


def classify_pending(repo: Path, conn: sqlite3.Connection, model: str,
                     session: Session, limit: int | None = None) -> list[int]:
    """Resume-aware rolling classification. Returns processed commit ids."""
    pending = conn.execute(
        """SELECT id, committed_at, walk_index FROM commits
           LEFT JOIN commit_verdicts v ON v.commit_id = commits.id
           WHERE v.commit_id IS NULL ORDER BY walk_index LIMIT ?""",
        (limit if limit is not None else -1,),
    ).fetchall()
    processed: list[int] = []
    for row in pending:
        msg = commit_prompt(conn, row["walk_index"])
        crow = conn.execute(
            "SELECT kind, diff FROM commits WHERE id = ?", (row["id"],)
        ).fetchone()
        assert crow is not None
        if crow["kind"] == "merge" and not crow["diff"]:
            with conn:
                conn.execute(
                    "INSERT INTO commit_verdicts"
                    " (commit_id, verdict, rationale, raw_llm_output, model, classified_at)"
                    " VALUES (?, 'merge', 'empty diff vs first parent', '', ?, datetime('now'))",
                    (row["id"], model),
                )
            continue
        text = session.send(msg)
        try:
            reply = parse_reply(text)
        except (ValueError, json.JSONDecodeError, IndexError) as exc:
            repair = session.send(
                f"Your last reply was not one JSON object line ({exc.__class__.__name__})."
                " Reply with exactly one JSON object line for the SAME commit, nothing else."
            )
            try:
                reply = parse_reply(repair)
                text = repair
            except (ValueError, json.JSONDecodeError, IndexError):
                with conn:
                    conn.execute(
                        "INSERT INTO commit_verdicts"
                        " (commit_id, verdict, rationale, raw_llm_output, model, classified_at)"
                        " VALUES (?, 'unknown', 'unparsable LLM reply', ?, ?, datetime('now'))",
                        (row["id"], text, model),
                    )
                processed.append(row["id"])
                continue
        apply_reply(conn, row["id"], row["committed_at"], reply, text, model)
        processed.append(row["id"])
    return processed
