# skill-stats — design (post-grill)

Tool that mines git history (+ later opencode history) to measure feature-level
metrics: features/year, fixes/feature (overall, per-feature), code churn per
feature; later cost/feature/model, cost/skill, fixes/feature/skill,
fixes/feature/model, skill quality over time.

Pilot repo: ~/dotfiles (183 commits since 2025-09 main; 290 opencode sessions —
richest pairing). Store: imported SQLite copy. Explore via python (uv) +
matplotlib scripts over SQL queries. Build git-only phase first.

## Git pipeline

- **Walk scope**: `main` only (or the Target branch). Squash-merged branch
  commits appear once, as the main-branch squash commit. Patch-equivalent
  commits on main are reconciled via patch-id.
- **Commit verdict**: each commit is classified once, by the LLM rolling walk:
  `feature | fix | refactor | revert | cleanup` (+ `merge` kind for merges).
  Mechanical blame detection of line-deletion is the *evidence*, not the
  verdict; formatting-only changes classify as `cleanup`, reverts as `revert`
  (never as fix). Feature metrics (features/year, fixes/feature) count only
  `feature`-verdict commits in the numerator of features/year; fixes are
  `fix`-verdict commits.
- **Feature**: coherent unit of change; LLM-classified. Rolling job via opencode
  CLI: walk commits chronologically on main, ask "new feature or existing one?",
  checkpoint/resumable; only new commits classified. Most dotfiles features = 1
  commit; schema is many-to-many anyway (a commit may define multiple features;
  a commit may both define a feature and be a fix's target).
- **Fix**: a `fix`-verdict commit that modifies lines belonging to existing
  commits (of a feature or another fix). Detection: deletion/rewrite lines vs
  parent (merge commits use first parent on the Target branch), blamed
  (`blame -w -C`) at the parent to a commit that defines a feature or a fix.
- **Targets**: a fix may target several commits. Tables:
  `fix_touches(fix_commit_id, source_commit_id)` raw blame hits (commit
  granularity, no line ranges), and
  `fix_targets(fix_commit_id, target_commit_id)` many-to-many.
- **Lineage**: a fix that targets a fix is attributed transitively: to each fix
  it targets and up-chain to the original feature(s).
  `fixes_features` precomputed transitive closure so queries avoid recursive
  CTEs. Metrics count a fix once per feature it reaches.
- **Identity**: feature id assigned once at creation (keyed to defining
  commits); canonical title locked, later titles kept as history. Incremental
  runs only classify new commits. Raw LLM verdicts stored per commit for
  auditability.
- **User metrics**: dropped for now (single human, 5 author-strings). Author
- normalization is out of scope.

## opencode pipeline (schema settled now, built later)

- New threads only: analysis covers sessions created after the plugin exists;
  every session predating the plugin within the db span (which begins
  2026-03-24) is historical and excluded from skill metrics.
- Plugin (future work): V2 plugin API only — V1 plugin implementations do
  not run in V2 (`Plugin.define` + `setup(ctx)`, hook via
  `ctx.tool.hook("execute.before"/"after")` or `ctx.event.subscribe()`).
  Log at skill load — skill dir content hash, session and message ids,
  source (user-invoked vs auto), context size. This is the authoritative
  capture; without it, skill metrics by build only explicit `skill` tool
  calls. Verify at build: `ctx.skill` may expose skill identity/paths
  (possibly replacing manual dir hashing), and how to observe
  user-invoked vs auto source — in V2 recorded data the skill part carries
  only `{name}`, so source detection needs the prompt/session hook and its
  mechanism is not yet proven; fall back to context-size heuristics if
  source detection fails.
- Built-in skills (e.g. customize-opencode) out of scope — we don't own or
  improve them. Skills with no local dir (5 of 47 names seen) are 'external/
  unknown-version' and excluded from version-tracking metrics.
- Import (normalized copy) from the db resolved via `opencode debug paths db`
  (path respects release channel and `OPENCODE_DB`; never hardcode). Keep
  source ids. opencode ≥ V2 (v2.0.x, event-sourced): `session_v2` is the
  authoritative session table (strict superset of legacy `session`); message
  and part rows store JSON blobs — assistant cost/tokens/modelID/providerID
  live in `message.data`, so the importer extracts via `json_extract` rather
  than flat columns. The V1-era 97.6% message population figure must be
  re-measured against the V2 blobs before the importer is built.
- **Skill invocation**: explicit `skill` tool call. Unit of skill metrics.
  In V2 parts, the skill name is `state.input.name` (not `skill`); the blob
  shape may change again — treat `part.data` as data to json_extract, never
  string-match. Recorded subagent tool calls are still named `task`
  (460 parts present); parent_id session linking holds in `session_v2`.
- **Skill cost**: cost from the message that invokes the skill until end of
  session (approximation), including costs from subagents the skill spawned
  (task tool → subagent sessions, with parent_id linking; errored subagents
  flagged, their partial cost excluded).

## Adjudicated answers during grill

- fixes/feature/specific-fix: fixes counted once each, attributed to every unit
  in lineage: target_size matches fixes/feature of fix-F when requested.
- Cost: per-message rollup; session totals convenience only.
- ADR-0001: LLM feature/classification via opencode CLI.
- ADR-0002: skill metrics require plugin-logged data; no backfill attempt.
- Steers: classify steers per skill directly by the LLM (not by interval rule).
