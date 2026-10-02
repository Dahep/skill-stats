# ADR 0001: LLM-based feature classification via opencode CLI

## Status: accepted

## Context & decision

Feature identification is a fuzzy, judgment-laden task — commit titles and diffs
overlap, a fix for yesterday's feature looks like a feature today. We decided to
classify commits into Features by feeding them (title, message, diff) to an LLM
through the opencode CLI, running a rolling session: process commits
chronologically, ask "new Feature or existing one?", checkpointing the feature
list so runs are resumable and only new commits get classified.

## Considered Options

- **Pure heuristics** (title/diff ratios, prefixes): deterministic and free, but
  collapses on merged multi-concern commits and cannot give features meaningful
  canonical titles.
- **Manual curation**: highest quality, but doesn't scale to monitoring a repo
  continuously, defeats the point of metrics a tool collects.
- **Per-commit batch prompt** (one LLM call commits→features): cheaper, but loses
  the chronological "new or existing?" framing and cannot be checkpointed.

## Consequences

- Classification quality is bounded by prompt + model; misattributions are
  possible and should be inspectable/correctible via the DB.
- Runs are incremental and resumable; feature IDs persist across runs.
