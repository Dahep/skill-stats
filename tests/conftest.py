"""Shared fixture: a tiny repo with known feature/fix/refactor/revert shape.

History (chronological, all on main):
  c1 "feature one"   a.txt: alpha/beta/charlie          -> feature 1
  c2 "feature two"   b.txt: one/two                     -> feature 2
  c3 "fix alpha"     a.txt rewrites alpha->ALPHA        -> fix (targets c1)
  c4 "refactor"      a.txt reorders lines               -> refactor (no fix rows)
  c5 "fix of fix"    a.txt rewrites ALPHA->ALPHAV2      -> fix (targets c3; lineage -> c1)
  c6 "dup add"       adds dup.txt with same patch       -> cleanup-ish (patch-id dupe of c7)
  c7 "add dup.txt"   dup.txt introduced                 -> patch-equivalent with c6
  c8 "whitespace"    a.txt adds trailing newline only   -> cleanup (no deleted lines)
"""

import subprocess
from pathlib import Path

import pytest

from skill_stats.classify import create_feature
from skill_stats.db import connect
from skill_stats.identity import register_repository


def run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def commit_files(repo: Path, msg: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run(repo, "add", "-A")
    run(repo, "commit", "-m", msg)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_db(path: Path, repo: Path, branch: str = "main"):
    """A fresh DB with THE repository row registered (wave 1: one repo per DB)."""
    conn = connect(path)
    register_repository(conn, repo, branch)
    return conn


def add_feature(conn, title: str, about: str = "", created_at: str = "2026-01-01T00:00:00Z") -> str:
    """Insert a feature with the repo-prefixed id for the DB's single repo."""
    repo_id = conn.execute("SELECT id FROM repositories ORDER BY id").fetchone()[0]
    return create_feature(conn, repo_id, title, about, created_at)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    run(r, "init", "-b", "main")
    run(r, "config", "user.name", "Tester")
    run(r, "config", "user.email", "t@t.io")
    run(r, "config", "commit.gpgsign", "false")
    return r


@pytest.fixture()
def shas(repo: Path) -> dict[str, str]:
    history: dict[str, str] = {}
    history["c1"] = commit_files(
        repo,
        "feature one: alpha module",
        {
            "a.txt": "alpha\nbeta\ncharlie\n",
        },
    )
    history["c2"] = commit_files(
        repo,
        "feature two: beta index",
        {
            "b.txt": "one\ntwo\n",
        },
    )
    history["c3"] = commit_files(
        repo,
        "fix alpha handling",
        {
            "a.txt": "ALPHA\nbeta\ncharlie\n",
        },
    )
    history["c4"] = commit_files(
        repo,
        "refactor: reorder alpha module",
        {
            "a.txt": "charlie\nALPHA\nbeta\n",
        },
    )
    history["c5"] = commit_files(
        repo,
        "fix: actually repair alpha v2",
        {
            "a.txt": "charlie\nALPHAV2\nbeta\n",
        },
    )
    # patch-id duplicate setup: introduce dup.txt, remove it, re-add the same content
    history["c6"] = commit_files(
        repo,
        "add dup suport",
        {
            "dup.txt": "dup\nextra\n",
        },
    )
    (repo / "dup.txt").unlink()
    run(repo, "add", "-A")
    run(repo, "commit", "-m", "remove dup file")
    history["cr"] = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    history["c7"] = commit_files(
        repo,
        "add dup support",
        {
            "dup.txt": "dup\nextra\n",
        },
    )
    # whitespace-only change (adds a blank line): no deletions, no blame targets
    history["c8"] = commit_files(
        repo,
        "tidy: blank line",
        {
            "a.txt": "charlie\n\nALPHAV2\nbeta\n",
        },
    )
    return history


@pytest.fixture()
def dbconn(tmp_path: Path):
    return connect(tmp_path / "test.db")
