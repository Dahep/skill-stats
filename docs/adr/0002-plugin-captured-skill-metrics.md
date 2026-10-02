# ADR 0002: Skill metrics require plugin-logged invocations

## Status: accepted

## Decision

Skill-stats covers only Skill invocations captured going forward by a plugin
that fires on skill load and logs: skill directory content hash, session and
message ids, invocation source (user-invoked vs auto-triggered), and context
size at load. Sessions that predate the plugin are out of scope for all skill
metrics — no backfill attempt will be made.

Built-in skills (not owned or improved by us) and skills with no local
directory are also excluded from version-tracking metrics.

## Why

Historical sessions lack the data these metrics depend on: the opencode db is
truncated anyway (starts 2026-03-24), skill directories cannot be faithfully
reconstructed for every past invocation, and invocation source (user vs auto)
is not derivable from the recorded tool-call payload. A plugin captures the
ground truth at the moment it happens.
