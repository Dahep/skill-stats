"""Artifact serializer tests: determinism, curation, digest verify, materialization."""

import hashlib
import json
import re
import sqlite3

from conftest import add_feature, commit_files, make_db
from skill_stats import artifact
from skill_stats.gitwalk import walk
from skill_stats.lineage import close_lineage
from skill_stats.livelines import update_live_lines
from skill_stats.targets import annotate_all

CLASSIFIED_AT = "2026-01-01T00:00:00Z"


def _verdict(conn, sha, verdict):
    conn.execute(
        "INSERT INTO commit_verdicts (commit_id, verdict, rationale, raw_llm_output, model,"
        " classified_at) VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'why', 'raw', 'm',"
        " ?)",
        (sha, verdict, CLASSIFIED_AT),
    )


def _history(repo):
    h1 = commit_files(repo, "feature a", {"f.txt": "a1\na2\n"})
    h2 = commit_files(repo, "fix a1", {"f.txt": "A1\na2\n"})
    return h1, h2


def _built_db(tmp_path, repo, name="a.db", hist=None):
    """A working DB with verdicts, features, fix lineage and live-lines samples."""
    h1, h2 = hist or _history(repo)
    conn = make_db(tmp_path / name, repo)
    walk(repo, conn, "main")
    fa = add_feature(conn, "feature a")
    conn.execute(
        "INSERT INTO commits_features (commit_id, feature_id, role)"
        " VALUES ((SELECT id FROM commits WHERE sha = ?), ?, 'defines')",
        (h1, fa),
    )
    for sha, v in ((h1, "feature"), (h2, "fix")):
        _verdict(conn, sha, v)
    conn.commit()
    annotate_all(repo, conn)
    close_lineage(conn)
    update_live_lines(conn, repo)
    conn.commit()
    return conn, fa, h1, h2


def _header(content: str, key: str) -> str:
    m = re.search(rf"^-- {key}: ?(.*)$", content, re.M)
    assert m is not None
    return m.group(1)


def test_serialize_is_byte_stable_and_digest_verifies(tmp_path, repo):
    conn, *_ = _built_db(tmp_path, repo)
    first = artifact.serialize(conn)
    second = artifact.serialize(conn)
    assert first == second  # byte-equal across calls
    path = artifact.write(conn, repo)
    assert path == repo / ".skill-stats" / "skill-stats.sql"
    assert (repo / ".skill-stats" / "report.html").exists()
    assert artifact.verify(path) is None
    content = path.read_text(encoding="utf-8")
    assert artifact.digest_of(path) == _header(content, "digest")
    # tampering with any byte breaks verification
    tampered = tmp_path / "tampered.sql"
    body = bytearray(first)
    body[len(body) // 2] ^= 0x01
    tampered.write_bytes(bytes(body))
    err = artifact.verify(tampered)
    assert err is not None and "mismatch" in err
    # a missing digest line is an error, not an ok
    bare = tmp_path / "bare.sql"
    bare.write_text("-- skill-stats artifact\n", encoding="utf-8")
    assert artifact.verify(bare) is not None
    conn.close()


def test_serialize_stable_across_identically_built_dbs(tmp_path, repo):
    # two DBs over the same history: wall-clock (repositories.added_at etc.)
    # must not leak into the artifact
    hist = _history(repo)
    conn_a, *_ = _built_db(tmp_path, repo, "a.db", hist)
    conn_b, *_ = _built_db(tmp_path, repo, "b.db", hist)
    assert artifact.serialize(conn_a) == artifact.serialize(conn_b)
    conn_a.close()
    conn_b.close()


def test_artifact_curation_excludes_machine_state(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    conn.execute("INSERT INTO runs (config_json, started_at, updated_at) VALUES ('{}', 'x', 'y')")
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('repo', ?)",
        (json.dumps(str(repo)),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('classify_session_id', '\"ses_x\"')"
    )
    conn.commit()
    content = artifact.serialize(conn).decode("utf-8")

    # runs table and machine-local settings are out entirely
    assert "INSERT INTO runs" not in content and "CREATE TABLE runs" not in content
    assert "classify_session_id" not in content
    assert str(repo) not in content
    # wall-clock columns frozen to sentinels, raw diff payload emptied
    assert "datetime('now')" not in content
    assert "NULL, '');" in content  # repositories.added_at sentinel
    assert "a1\\na2" not in content and "A1\\na2" not in content  # no diff payload
    # classified_at is kept: classified-once audit data, not churn
    assert CLASSIFIED_AT in content
    conn.close()


def test_artifact_materializes_standalone_with_committed_shapes(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    path = artifact.write(conn, repo, with_report=False)
    mat = sqlite3.connect(tmp_path / "materialized.db")
    mat.row_factory = sqlite3.Row
    mat.executescript(path.read_text(encoding="utf-8"))

    tables = {r[0] for r in mat.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "runs" not in tables  # curated projection
    assert {
        "repositories",
        "commits",
        "commit_verdicts",
        "features",
        "commits_features",
        "fix_touches",
        "fix_targets",
        "fixes_features",
        "feature_line_samples",
        "settings",
    } <= tables

    # drift guard: the artifact's shape matches the working tables exactly
    for table in sorted(tables):
        work_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        mat_cols = [r["name"] for r in mat.execute(f"PRAGMA table_info({table})")]
        assert mat_cols == work_cols, table

    assert mat.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"] == 2
    assert mat.execute("SELECT COUNT(*) c FROM features").fetchone()["c"] == 1
    row = mat.execute("SELECT * FROM repositories").fetchone()
    assert row["added_at"] == ""  # sentinel, not wall-clock
    diff = mat.execute(
        "SELECT diff, added_lines, deleted_lines, churn_lines FROM commits ORDER BY id"
    ).fetchall()
    assert all(r["diff"] == "" for r in diff)
    assert (diff[0]["added_lines"], diff[0]["deleted_lines"]) == (2, 0)
    assert mat.execute("SELECT COUNT(*) c FROM feature_line_samples").fetchone()["c"] > 0
    keys = {r["key"] for r in mat.execute("SELECT key FROM settings")}
    assert keys <= {"branch", "model", "min_target_lines", "unattributed_lines"}
    assert (
        mat.execute("SELECT COUNT(*) c FROM commit_verdicts WHERE run_id IS NOT NULL").fetchone()[
            "c"
        ]
        == 0
    )
    conn.close()
    mat.close()


def test_header_fields(tmp_path, repo):
    conn, fa, h1, h2 = _built_db(tmp_path, repo)
    content = artifact.serialize(conn).decode("utf-8")
    assert content.startswith("-- skill-stats artifact\n-- format: 1\n")
    assert _header(content, "schema-step") == "2"
    assert _header(content, "repository-key") == "local:repo@main"
    assert _header(content, "covered-through") == h2  # latest walked commit
    digest = _header(content, "digest")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    # digest covers the content with its own value blanked
    blanked = re.sub(r"^-- digest: .*$", "-- digest:", content, count=1, flags=re.M)
    assert hashlib.sha256(blanked.encode("utf-8")).hexdigest() == digest
    conn.close()
