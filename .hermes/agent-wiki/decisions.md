# Durable Decisions

## 2026-07-22 — KIS is the primary price source

- **Decision:** Use KIS for price, volume, trading value, and market-cap evidence in operational research pipelines.
- **Preserve:** KRX annual calendar remains the basis for scheduled trading-day and previous-business-date calculations; NXT and KIND retain their respective roles.
- **Reason:** The deployed KRX authenticated daily-price workflow returned safe diagnostic `KRX HTTP 401`; the user selected KIS rather than continued authentication speculation.
- **Constraint:** KIS must be identified as KIS, never represented as KRX exchange evidence. Credentials/tokens remain out of URLs, artifacts, exceptions, commits, and reports.

## 2026-07-23 — Public Wiki and agent operating wiki are separate

- `wiki/` is the deployed research source of truth.
- `.hermes/agent-wiki/` is private, durable operating memory and is excluded from public reports and Wiki deployment.

## 2026-07-23 — Completion is evidence, not progress narration

A source migration is complete only after exact reviewed SHA, remote push, CI, credential-backed workflow reaching report generation, source Wiki commit, and exact Wiki publication are observed.
