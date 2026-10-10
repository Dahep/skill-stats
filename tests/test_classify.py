"""Classification protocol tests: reply parsing, apply effects, resume semantics."""

import pytest

from conftest import add_feature, make_db
from skill_stats.classify import (
    Reply,
    apply_reply,
    classify_pending,
    commit_prompt,
    feature_registry,
    parse_reply,
)
from skill_stats.gitwalk import walk


class FakeSession:
    """Scripted runner: pops one reply per send."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.session_id = None

    def send(self, message: str) -> str:
        self.prompts.append(message)
        return self.replies.pop(0)


@pytest.fixture()
def walked(repo, shas, tmp_path):
    conn = make_db(tmp_path / "c.db", repo)
    walk(repo, conn, "main")
    return conn


def _ids_by_sha(conn):
    return {r["sha"]: r["id"] for r in conn.execute("SELECT id, sha FROM commits")}


def test_parse_reply_accepts_bare_and_fenced():
    assert parse_reply('{"k":"new","title":"Telemetry","about":"x","why":"y"}').kind == "new"
    assert parse_reply('```json\n{"k":"fix","why":"r"}\n```').kind == "fix"


def test_parse_reply_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown k"):
        parse_reply('{"k":"nope"}')


def test_parse_reply_takes_first_line():
    assert parse_reply('{"k":"cleanup","why":"w"}\ntrailing noise').kind == "cleanup"


def test_apply_new_feature(walked):
    ids = _ids_by_sha(walked)
    first_commit_id = min(ids.values())  # walk_index 1
    reply = Reply(
        "new", {"k": "new", "title": "alpha module", "about": "first module", "why": "coherent"}
    )
    verdict = apply_reply(walked, first_commit_id, "2026-01-01T00:00:00Z", reply, "{}", "m")
    assert verdict == "feature"
    feat = walked.execute("SELECT * FROM features").fetchone()
    assert feat["title"] == "alpha module"
    # repo-prefixed feature id: {slug}-{local number} (ADR-0003 clause 5)
    assert feat["id"] == "repo-1"
    link = walked.execute(
        "SELECT role FROM commits_features WHERE feature_id = ?", (feat["id"],)
    ).fetchone()
    assert link["role"] == "defines"
    v = walked.execute("SELECT verdict FROM commit_verdicts").fetchone()
    assert v["verdict"] == "feature"


def test_apply_existing_feature(walked, shas, repo):
    ids = _ids_by_sha(walked)
    fid = add_feature(walked, "alpha")
    assert fid == "repo-1"
    reply = Reply("existing", {"k": "existing", "f": fid, "why": "continues scope"})
    verdict = apply_reply(walked, ids[shas["c2"]], "2026", reply, "{}", "m")
    assert verdict == "feature"
    role = walked.execute(
        "SELECT role FROM commits_features WHERE feature_id = ?", (fid,)
    ).fetchone()["role"]
    assert role == "touches"
    # citing an unknown feature id raises loudly
    with pytest.raises(ValueError, match="unknown feature id"):
        bad = Reply("existing", {"k": "existing", "f": "repo-999", "why": "x"})
        apply_reply(walked, ids[shas["c3"]], "2026", bad, "", "m")


def test_feature_ids_are_prefixed_and_increment(walked, shas):
    ids = _ids_by_sha(walked)
    first = min(ids.values())
    apply_reply(
        walked,
        first,
        "2026-01-01T00:00:00Z",
        Reply("new", {"k": "new", "title": "one", "about": "", "why": "y"}),
        "{}",
        "m",
    )
    apply_reply(
        walked,
        ids[shas["c2"]],
        "2026-01-02T00:00:00Z",
        Reply("new", {"k": "new", "title": "two", "about": "", "why": "y"}),
        "{}",
        "m",
    )
    fids = [r[0] for r in walked.execute("SELECT id FROM features ORDER BY rowid")]
    assert fids == ["repo-1", "repo-2"]


def test_prefixed_citation_through_prompt_and_parse(walked, shas):
    ids = _ids_by_sha(walked)
    apply_reply(
        walked,
        min(ids.values()),
        "2026-01-01T00:00:00Z",
        Reply("new", {"k": "new", "title": "alpha module", "about": "first", "why": "y"}),
        "{}",
        "m",
    )
    reg = feature_registry(walked)
    assert "repo-1:" in reg and "alpha module" in reg
    prompt = commit_prompt(walked, 2)
    assert "FEATURES" in prompt and "repo-1" in prompt
    reply = parse_reply('{"k":"existing","f":"repo-1","why":"continues scope"}')
    assert reply.kind == "existing" and reply.payload["f"] == "repo-1"
    apply_reply(walked, ids[shas["c2"]], "2026", reply, "{}", "m")
    row = walked.execute(
        "SELECT role FROM commits_features WHERE feature_id = 'repo-1' AND commit_id = ?",
        (ids[shas["c2"]],),
    ).fetchone()
    assert row["role"] == "touches"


def test_classify_pending_processes_in_order_and_stores_raw(walked, shas):
    replies = [
        '{"k":"new","title":"alpha module","about":"first","why":"defs"}',
        '{"k":"new","title":"beta index","about":"second","why":"defs2"}',
        '{"k":"fix","why":"repairs alpha"}',
        '{"k":"refactor","why":"reorder"}',
        '{"k":"fix","why":"repair of previous fix"}',
        '{"k":"new","title":"dup support","about":"x","why":"defs"}',
        '{"k":"new","title":"remove dup","about":"x","why":"defs"}',
        '{"k":"fix","why":"repair of previous fix"}',
        '{"k":"cleanup","why":"whitespace"}',
    ]
    session = FakeSession(replies)
    done = classify_pending("/dev/null-repo-unused", walked, "m", session)
    assert len(done) == 9
    verdicts = [
        r[0]
        for r in walked.execute(
            "SELECT v.verdict FROM commit_verdicts v JOIN commits c ON c.id = v.commit_id"
            " ORDER BY c.walk_index"
        ).fetchall()
    ]
    expected = [
        "feature",
        "feature",
        "fix",
        "refactor",
        "fix",
        "feature",
        "feature",
        "fix",
        "cleanup",
    ]
    assert verdicts == expected
    titles = [r[0] for r in walked.execute("SELECT title FROM features ORDER BY rowid").fetchall()]
    assert titles == ["alpha module", "beta index", "dup support", "remove dup"]


def test_classify_pending_is_resumable(walked, shas):
    session = FakeSession(
        [
            '{"k":"new","title":"alpha module","about":"first","why":"defs"}',
            '{"k":"new","title":"beta index","about":"second","why":"defs2"}',
        ]
    )
    classify_pending("/dev/null-repo-unused", walked, "m", session, limit=2)
    assert len(walked.execute("SELECT * FROM commit_verdicts").fetchall()) == 2
    # resume: remaining 7 commits, with a session repair to boot
    session2 = FakeSession(
        [
            '{"k":"fix","why":"r"}',
            "garbage",
            '{"k":"refactor","why":"r"}',
            '{"k":"fix","why":"r"}',
            '{"k":"new","title":"dup","about":"x","why":"d"}',
            '{"k":"new","title":"remove","about":"x","why":"d"}',
            '{"k":"fix","why":"r"}',
            '{"k":"cleanup","why":"w"}',
        ]
    )
    done = classify_pending("/dev/null-repo-unused", walked, "m", session2)
    assert len(done) == 7
    roles = [
        r[0]
        for r in walked.execute(
            "SELECT v.verdict FROM commit_verdicts v JOIN commits c ON c.id = v.commit_id"
            " ORDER BY c.walk_index"
        ).fetchall()
    ]
    assert roles[:2] == ["feature", "feature"]
    assert "unknown" in roles or roles[-1] == "cleanup"


def test_classify_pending_unparsable_marks_unknown(walked, shas):
    session = FakeSession(
        [
            "not json at all\neither",
            "still not json",
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
            '{"k":"cleanup","why":"w"}',
        ]
    )
    done = classify_pending("/dev/null-repo-unused", walked, "m", session)
    assert len(done) == 9
    first_verdict = walked.execute(
        "SELECT v.verdict FROM commit_verdicts v JOIN commits c ON c.id = v.commit_id"
        " WHERE c.walk_index = 1"
    ).fetchone()[0]
    assert first_verdict == "unknown"


def test_prompt_contains_commit_and_features(walked):
    reg = feature_registry(walked)
    assert reg == "(none yet)"
    prompt = commit_prompt(walked, 1)
    assert "feature one: alpha module" in prompt
    assert "FEATURES" in prompt
