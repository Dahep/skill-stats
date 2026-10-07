# ADR 0002: Skill metrics require plugin-logged invocations

## Status: accepted

## Note (2026-10, post V2 migration)

With opencode V2, the capture plugin must be a V2 plugin: V1 plugin
implementations do not run in V2. Concretely `Plugin.define` with an
`id` and `setup(ctx)`, hooking `ctx.tool.hook("execute.before"/"after")`
or subscribing via `ctx.event.subscribe()`; plugin state goes through
`ctx.storage`, plugin files live in `.opencode/plugins/`.

Two V2 capabilities to verify at build time, and their fallbacks:

- **Skill identity/hash**: V2 exposes a `ctx.skill` domain that may provide
  skill identity/paths directly; prefer that, fall back to hashing the
  skill's directory contents as originally planned.
- **Invocation source (user vs auto)**: in V2's recorded data the `skill`
  tool part carries only `{name}` — no source marker. Capturing source
  requires correlating `execute.before` with a prompt/session hook, whose
  exact mechanism is not yet proven. If it cannot be captured reliably,
  fall back to context-size heuristics at import time; the steering-by-LLM
  classification (per the grill adjudications) still works without source.

Related V2 import facts (see DESIGN.md): sessions read from `session_v2`;
per-message cost/tokens/model live in `message.data` JSON blobs; the db path
is resolved via `opencode debug paths db`.

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
