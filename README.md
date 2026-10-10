# skill-stats

Mine git history to measure feature-level metrics: **features/year**,
**fixes/feature** (overall and per feature), and **code churn per feature**.
See `DESIGN.md` (decisions) and `CONTEXT.md` (domain language).

## Git pipeline (built)

1. `walk` — first-parent chronological walk of the Target branch (`main`).
   Squash-merged work appears once as its main-branch commit; `git patch-id`
   is stored per commit so patch-equivalent commits can be reconciled.
2. `classify` — rolling LLM session via the opencode CLI (ADR-0001): one
   commit per turn, one JSON reply per turn (`new | existing | fix |
   refactor | revert | cleanup`), checkpointed into SQLite per turn so runs
   are resumable. Raw LLM output is stored per commit for auditability; the
   single LLM verdict drives what metrics count (blame evidence never does).
3. `detect` — for `fix`-verdict commits only: blame deleted/rewritten lines
   at the first parent (`git blame -w -C --first-parent --porcelain`) and
   record `fix_touches` (raw hits) and `fix_targets` (hits ≥
   `min_target_lines`).
4. `lineage` — `fixes_features` transitive closure: a fix of a fix is
   attributed to every unit in its chain, each fix counted once per feature
   it reaches (`via_fix_commit_id` marks the first intermediate fix).
5. `report` — features/year, fixes/feature, churn per feature; text, JSON
   (`--json`), matplotlib charts (`--charts`), and a self-contained HTML page
   (`--html [path]`: fix ranking, features-over-time, 1-month rolling
   fixes/feature, verdict mix, commit volume, churn).

## Usage

```sh
uv sync
uv run skill-stats init ~/dotfiles --db ~/.skill-stats/skill-stats.db
uv run skill-stats walk    --db ~/.skill-stats/skill-stats.db
uv run skill-stats classify --db ~/.skill-stats/skill-stats.db [--limit N] [--fresh]
uv run skill-stats detect  --db ~/.skill-stats/skill-stats.db
uv run skill-stats lineage --db ~/.skill-stats/skill-stats.db
uv run skill-stats report  --db ~/.skill-stats/skill-stats.db --json --charts
```

`classify` shells out to the `opencode` CLI (`opencode run --format json`,
model configurable at `init` with `--model`, default `opencode/glm-5.3-flash`)
in its own scratch session dir so it never touches other sessions. If a reply
is not one JSON object line, it asks the model to repair once; unrecoverable
replies are stored as verdict `unknown` and excluded from metrics.

## Not built yet (per DESIGN.md)

- Artifact store (ADR-0003: committed `.skill-stats/` SQL snapshot + digest +
  HTML report), `update` verb, exclusion filter, live-lines backfill, CI
  scaffold, schema step 1 (repo-scoped ids).
- Gather / union DB org-wide reports.
- opencode-history import (schema is settled, built later).
- Plugin-logged skill metrics (ADR-0002: going-forward only).

## Development

```sh
uv run pytest            # full suite
uv run pytest tests/test_targets_lineage.py   # single file
uv run mypy src          # strict
uv run ruff check src tests
```
