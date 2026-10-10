# skill-stats

A tool that mines git history (and later opencode history) to measure feature-level metrics like features/year, fixes/feature, and later cost and skill usage per feature.

## Language

### Git domain

**Repository key**:
A Target repo's identity across machines and clones: clone URL plus the repository's Target branch. Machine-local paths are never identity; Gather keys on a Repository key, two clones of the same repository are one.

**Feature id**:
A Feature's identifier. Locally unique within a repo; globally disambiguated by its Repository key so gathered unions need no remapping.

**Fix**:
A commit that modifies lines belonging to existing commits (of a Feature or of another Fix) without adding a Feature.
_Avoid_: bugfix, patch

**Fix lineage**:
The relation formed by fixes that target other fixes. A Fix is attributed to every Feature its chain reaches: the Fixes it targets and, transitively, the original Feature(s) — a fix may target several units and so reach several features. Metrics count each Fix once per Feature it reaches.

**Feature attribution**:
The mapping of a commit onto a Feature: directly (the commit defines the feature) or via blame (the commit touches the feature's lines).

**Commit verdict**:
The single LLM-assigned class of a commit: Feature, Fix, Refactor, Revert, or Cleanup. Blame evidence feeds it, but exactly one verdict drives which metrics count the commit.
_Avoid_: commit type, category

**Refactor**:
A Commit verdict for a change that restructures existing Feature lines without defect repair intent.
_Avoid_: clean-up, rewrite

**Revert**:
A Commit verdict for a change that undoes an earlier commit (or fixes a revert). A Revert is never counted as a Fix.
_Avoid_: rollback, undo

**Cleanup**:
A Commit verdict for formatting/whitespace/non-semantic changes.
_Avoid_: style change, lint commit

### opencode domain

**Skill invocation**:
One explicit `skill` tool call recorded in opencode history, whether user-invoked or automatically triggered. The atomic unit of all skill metrics.
_Avoid_: skill use, skill call

**Skill version**:
The content hash of a skill's directory, ideally captured at load time by a plugin. The identity skills carry in metrics like performance-per-version.

**Skill cost**:
The accumulated session cost from the message that loaded the Skill (invoking forward) until the skill's end — currently approximated as end of session — including costs incurred by subagents the skill spawned.

**Steer**:
A user message after a Skill invocation that redirects the agent, as opposed to confirming/approving or continuing. Classified by an LLM from the skill's context plus surrounding messages.

**Historical invocation**:
A Skill invocation from sessions predating the plugin's existence. Most skill attributes cannot be measured for these; Skill-invocation stats cover new Invocations only.

**Target repo**:
The git working copy a Run analyzes. One checkout pairs one Repository key.

**Artifact**:
The committed `.skill-stats/` store holding a Target repo's complete git-analysis state (verdicts, features, fix attribution, lineage). Source of truth for that repo's numbers; Org-wide metrics are gathered from many Artifacts.
_Avoid_: export, dump

**Artifact digest**:
A value stored inside the Artifact covering its whole content except the digest itself. Verification anchor for Updates and gathers: neither builds on an Artifact whose digest fails to verify.

**Coverage horizon**:
The commit up to which an Artifact version's data is trusted. Stored inside the Artifact; Partial recompute resumes after it.
_Avoid_: watermark, boundary

**Sample**:
One commit at which the live-lines history is recorded for every feature alive at that point. The curve is Samples joined; the latest Sample pins the per-feature summaries.
_Avoid_: checkpoint, snapshot

**Partial recompute**:
What an Artifact digest failure triggers: trust is rebuilt from the newest prior artifact version that still verifies, and work newer than it is re-analyzed. Falls back to a full recompute when no verifiable version survives.

**Target branch**:
The single branch a git analysis run walks (main or an explicitly configured alternative). All metrics are computed on Target-branch commits only.
_Avoid_: all branches, full history
