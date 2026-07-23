# Contract — KIS Primary Price-Source Migration

## Status

`paused-for-harness-bootstrap` — existing uncommitted KIS edits are not a release candidate and must be reviewed against this contract before reuse.

## Goal

Replace the deployed KRX authenticated daily-price operational path with a KIS-backed path that produces and publishes the same pre-market and post-market research outputs without misrepresenting KIS as KRX.

## Baseline / rollback

- Branch: `main`
- Baseline / `origin/main`: `4c4b6f96a3c93570b5dede06e4bf2b1cf90a37bc`
- Last known full-suite result before partial migration: 264 passed, coverage 90.13%.

## Required changes

1. KIS OAuth and exact-date watchlist daily-price artifact, with credentials/token non-serialization.
2. One KIS artifact contract consumed by pre-market and post-market candidate construction.
3. CLI revalidation and final release-gate validation of KIS price evidence, with the same independent evidence-to-signal binding depth currently required for KRX.
4. Pre-market and post-market workflows use `KIS_APP_KEY`, `KIS_APP_SECRET`, `collect-kis`, and KIS artifacts; KRX calendar collection remains.
5. Public methodology accurately labels KIS price evidence and KRX calendar evidence.
6. Tests, full gate, exact-SHA review, push, CI, credential-backed workflow, source Wiki commit, and GitHub Wiki verification.

## Must preserve

- `wiki/` remains the only public research source of truth.
- KRX annual calendar and previous-business-date fail-closed rules.
- NXT and KIND canonical evidence requirements.
- 07:30 KST pre-market schedule/guard policy unless separately changed by the user.
- Legacy manual KRX collectors stay available but are not used by production KIS workflows.
- No credential, OAuth token, provider response body, or reflected request may be persisted or printed.

## Non-goals

- Repository-wide evidence model redesign.
- Deleting legacy KRX collectors or morning/live paths.
- Changing scheduled-start guard policy.
- Generalized provider framework beyond the KIS production path.

## Acceptance criteria

```text
KIS credential → collect-kis → KIS artifact → candidate rebuild
→ final harness validation → report/Wiki output
```

must pass in local tests, then on an exact reviewed remote SHA through CI and a credential-backed Actions run to source Wiki and GitHub Wiki publication.

## Current next action

Inspect the partial working tree against this contract; retain only changes necessary for the single KIS tracer bullet or revert to the baseline before a new dedicated implementation branch/worktree is used.
