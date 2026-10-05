"""SQLite schema for skill-stats.

Migrations are ordered string steps applied to a fresh DB or existing file;
only step 0 exists for now. Add step 1+ for schema evolution.
"""

SCHEMA_STEPS: list[str] = [
    # 0: initial schema
    """
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        config_json TEXT NOT NULL,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT
    );

    -- settings as JSON key/value
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Target-branch commits only, in chronological walk order.
    CREATE TABLE commits (
        id INTEGER PRIMARY KEY,
        sha TEXT NOT NULL UNIQUE,
        parent_sha TEXT,              -- first parent on Target branch; NULL for root
        tree_sha TEXT NOT NULL,
        committed_at TEXT NOT NULL,   -- ISO-8601
        author_name TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        subject_patch_id TEXT,        -- git patch-id of the commit subject-quoted diff
        walk_index INTEGER NOT NULL UNIQUE,  -- 1-based chronological order
        kind TEXT NOT NULL CHECK (kind IN ('normal', 'merge')),
        diff TEXT NOT NULL
    );

    -- LLM-assigned Commit verdict; blame evidence is stored as evidence, not verdict.
    CREATE TABLE commit_verdicts (
        commit_id INTEGER PRIMARY KEY REFERENCES commits(id),
        verdict TEXT NOT NULL CHECK (verdict IN ('feature', 'fix', 'refactor', 'revert',
                                                 'cleanup', 'merge', 'unknown')),
        rationale TEXT NOT NULL,
        raw_llm_output TEXT NOT NULL,
        model TEXT NOT NULL,
        run_id INTEGER REFERENCES runs(id),
        classified_at TEXT NOT NULL
    );

    CREATE TABLE features (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        about TEXT NOT NULL DEFAULT '',
        title_history_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        created_run_id INTEGER REFERENCES runs(id)
    );

    -- Many-to-many: a commit may define several features; feature set may span commits.
    CREATE TABLE commits_features (
        commit_id INTEGER NOT NULL REFERENCES commits(id),
        feature_id INTEGER NOT NULL REFERENCES features(id),
        role TEXT NOT NULL CHECK (role IN ('defines', 'touches')),
        PRIMARY KEY (commit_id, feature_id),
        UNIQUE (feature_id, commit_id)
    );

    -- Raw blame hits at commit granularity.
    CREATE TABLE fix_touches (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        source_commit_id INTEGER NOT NULL REFERENCES commits(id),
        hit_lines INTEGER NOT NULL,
        PRIMARY KEY (fix_commit_id, source_commit_id)
    );

    -- Curated targets: sources with nontrivial overlap; may include other fixes.
    CREATE TABLE fix_targets (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        target_commit_id INTEGER NOT NULL REFERENCES commits(id),
        PRIMARY KEY (fix_commit_id, target_commit_id),
        FOREIGN KEY (target_commit_id) REFERENCES commits(id)
    );

    -- Transitive closure fix -> original feature(s); one row per (fix, feature) reach.
    CREATE TABLE fixes_features (
        fix_commit_id INTEGER NOT NULL REFERENCES commits(id),
        feature_id INTEGER NOT NULL REFERENCES features(id),
        -- NULL when the first-hop target defines the Feature directly
        via_fix_commit_id INTEGER REFERENCES commits(id),
        PRIMARY KEY (fix_commit_id, feature_id),
        UNIQUE (fix_commit_id, feature_id, via_fix_commit_id)
    );

    CREATE INDEX idx_commits_patch_id ON commits(subject_patch_id)
        WHERE subject_patch_id IS NOT NULL;
    CREATE INDEX idx_commits_features_feature ON commits_features(feature_id);
    CREATE INDEX idx_fix_targets_target ON fix_targets(target_commit_id);
    CREATE INDEX idx_fixes_features_feature ON fixes_features(feature_id);
    """,
]
