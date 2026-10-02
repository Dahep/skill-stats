# skill-stats

A tool that mines git history (and later opencode history) to measure feature-level metrics like features/year, fixes/feature, and later cost and skill usage per feature.

## Language

### Git domain

**Feature**:
A coherent unit of change in a repository, identified by grouping commits (title, message, diff) and classified by an LLM into a canonical feature title, stored in a features table.
_Avoid_: enhancement, ticket

**Fix**:
A commit that modifies lines belonging to existing commits (of a Feature or of another Fix) without adding a Feature.
_Avoid_: bugfix, patch

**Fix lineage**:
The chain formed by fixes of fixes. A Fix is attributed to every unit in its lineage: the Fix it targets and, transitively, the original Feature. Metrics count each unit once per lineage to avoid double-counting.

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

**Target branch**:
The single branch a git analysis run walks (main or an explicitly configured alternative). All metrics are computed on Target-branch commits only.
_Avoid_: all branches, full history
