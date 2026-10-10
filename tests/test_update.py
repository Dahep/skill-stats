"""update-verb tests: stage order, end-to-end artifact landing, backlog
visibility (Q13), trim flags, verify-first digest verdict."""

import re
import sqlite3

from conftest import commit_files
from skill_stats import artifact
from skill_stats.cli import main


class FakeSession:
    """Scripted classifier: pops one reply per send (no LLM)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.session_id = None

    def send(self, message: str) -> str:
        return self.replies.pop(0)


def _init(tmp_path, repo) -> str:
    db_path = str(tmp_path / "u.db")
    main(["init", str(repo), "--db", db_path])
    return db_path


def _two_commits(repo):
    commit_files(repo, "feature a", {"f.txt": "a1\n"})
    commit_files(repo, "feature b", {"g.txt": "b1\n"})


def test_update_end_to_end_lands_artifact(tmp_path, repo, capsys, monkeypatch):
    _two_commits(repo)
    db_path = _init(tmp_path, repo)
    replies = [
        '{"k":"new","title":"feature a","about":"x","why":"y"}',
        '{"k":"new","title":"feature b","about":"x","why":"y"}',
    ]
    monkeypatch.setattr("skill_stats.cli.Session", lambda model, sid=None: FakeSession(replies))

    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    # verify-first (missing artifact = ok first run), then the stage order
    assert "artifact: none yet" in out
    assert out.index("walked 2 new commits") < out.index("classified 2 commits")
    assert "2 walked, 2 classified, 0 pending, artifact updated" in out

    art_path = repo / ".skill-stats" / "skill-stats.sql"
    assert artifact.verify(art_path) is None
    assert (repo / ".skill-stats" / "report.html").exists()

    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.row_factory = sqlite3.Row
    mat.executescript(art_path.read_text(encoding="utf-8"))
    assert mat.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 2
    assert mat.execute("SELECT COUNT(*) c FROM features").fetchone()["c"] == 2
    assert mat.execute("SELECT COUNT(*) c FROM feature_line_samples").fetchone()["c"] > 0
    key = mat.execute("SELECT repo_key FROM repositories").fetchone()["repo_key"]
    assert key == f"local:{repo.name}@main"
    mat.close()


def test_update_backlog_is_visible_and_exit_zero(tmp_path, repo, capsys, monkeypatch):
    _two_commits(repo)
    db_path = _init(tmp_path, repo)

    class BrokenSession:
        session_id = None

        def send(self, message: str) -> str:
            raise RuntimeError("opencode CLI failed: no auth")

    monkeypatch.setattr("skill_stats.cli.Session", lambda model, sid=None: BrokenSession())
    main(["update", "--db", db_path])  # must not raise: exit 0 with backlog
    out = capsys.readouterr().out
    assert "WARNING: classification unavailable" in out
    assert "2 walked, 0 classified, 2 pending, artifact updated" in out
    assert "WARNING: 2 commits unclassified" in out
    # deterministic stages still ran and the artifact landed
    assert artifact.verify(repo / ".skill-stats" / "skill-stats.sql") is None
    mat = sqlite3.connect(tmp_path / "mat.db")
    mat.executescript((repo / ".skill-stats" / "skill-stats.sql").read_text(encoding="utf-8"))
    assert mat.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2
    mat.close()


def test_update_trim_flags(tmp_path, repo, capsys, monkeypatch):
    _two_commits(repo)
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "skill_stats.cli.Session",
        lambda model, sid=None: FakeSession(['{"k":"fix","why":"r"}'] * 2),
    )
    main(["update", "--db", db_path, "--no-classify", "--no-serialize", "--no-report"])
    out = capsys.readouterr().out
    assert "classified 2 commits" not in out  # the classify stage did not run
    assert "artifact not written" in out
    assert not (repo / ".skill-stats" / "skill-stats.sql").exists()
    assert not (repo / ".skill-stats" / "report.html").exists()
    mat = sqlite3.connect(db_path)
    assert mat.execute("SELECT COUNT(*) FROM commit_verdicts").fetchone()[0] == 0
    mat.close()


def test_update_logs_digest_verdict_of_existing_artifact(tmp_path, repo, capsys, monkeypatch):
    _two_commits(repo)
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "skill_stats.cli.Session",
        lambda model, sid=None: FakeSession(['{"k":"cleanup","why":"w"}'] * 2),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    art_path = repo / ".skill-stats" / "skill-stats.sql"
    body = bytearray(art_path.read_bytes())
    body[len(body) // 2] ^= 0x01
    art_path.write_bytes(bytes(body))

    main(["update", "--db", db_path])
    out = capsys.readouterr().out
    assert re.search(r"artifact digest: MISMATCH", out)
    # the rewrite supersedes the tampered file and verifies again
    assert artifact.verify(art_path) is None


def test_update_supersedes_non_utf8_corrupted_artifact(tmp_path, repo, capsys, monkeypatch):
    _two_commits(repo)
    db_path = _init(tmp_path, repo)
    monkeypatch.setattr(
        "skill_stats.cli.Session",
        lambda model, sid=None: FakeSession(['{"k":"cleanup","why":"w"}'] * 2),
    )
    main(["update", "--db", db_path])
    capsys.readouterr()
    art_path = repo / ".skill-stats" / "skill-stats.sql"
    art_path.write_bytes(art_path.read_bytes() + b"\xff\xfe garbage")  # invalid UTF-8

    main(["update", "--db", db_path])  # must continue, not abort
    out = capsys.readouterr().out
    assert "artifact digest: MISMATCH" in out
    assert "artifact updated" in out
    assert artifact.verify(art_path) is None
