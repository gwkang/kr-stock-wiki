# Agent Operating Contract — kr-stock-wiki

This file is the executable schema for any AI agent working in this repository. Read it before planning, editing, deploying, or reporting completion.

## Product boundary

- `wiki/` is the public, user-facing source of truth for published Korean-stock research.
- `.hermes/agent-wiki/` is private project-operating memory. It must never be copied into `wiki/`, reports, artifacts, or deployment output.
- Raw external sources and generated evidence are not interchangeable with agent summaries. Preserve authoritative source URLs and immutable evidence artifacts.

## Required operating sequence

1. Read `.hermes/agent-wiki/index.md`, the active contract, latest run log, and decisions.
2. Record a baseline: branch, HEAD, origin/main, working-tree state, and rollback SHA.
3. Implement one locked user-visible outcome only.
4. Prove one end-to-end tracer bullet before changing parallel paths.
5. Do not add edits on top of a failing or unreviewed working tree. Commit a coherent checkpoint or revert it.
6. Run focused tests, then the repository full gate.
7. Review an exact stable SHA before push.
8. Call work deployed only after remote SHA, CI, relevant workflow run, and Wiki publication evidence are observed.

## Scope control

A task contract must contain: Goal, Required changes, Must preserve, Non-goals, Acceptance criteria, rollback SHA, and status.

- “Implement all” completes every item needed for the stated outcome; it does not authorize unrelated redesign.
- Apply the Revert Test to every finding: if reverting the current diff leaves the issue, record it in `backlog.md` unless it directly blocks the locked acceptance criteria.
- A new subsystem, public schema, or provider contract requires an explicit contract update before edits.

## Communication

- If the user asks for completion-only updates, do not send progress reports.
- Never describe local partial code, a focused test, or an agent-helper launch as deployment.
- State only evidence-backed status: `local-only`, `committed-not-pushed`, `CI-pending`, `live-smoke-pending`, `verified-complete`, or a concrete external blocker.

## Tool preflight

Before an external agent/tool is used, verify it exists and is ready with read-only checks (for example `command -v codex`). Tool-launch failure must not be confused with a repository or deployment failure.
