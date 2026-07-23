# Agent Wiki Schema

## Layers

1. **Sources** (`sources/`): append-only manifest of external documents and authoritative URLs. Never silently alter a retrieved claim.
2. **Decisions** (`decisions.md`): durable, user-approved facts and rationale.
3. **Contracts** (`contracts/`): one file per active/recent user-visible outcome. A contract is the authority on scope.
4. **Runs** (`runs/`): append-only execution evidence: baseline, commands, observed results, SHA, next state.
5. **Backlog** (`backlog.md`): real issues deliberately excluded from the active contract.

## Contract template

```text
Status:
Goal:
Baseline / rollback SHA:
Required changes:
Must preserve:
Non-goals:
Acceptance criteria:
Current next action:
```

## Run log template

```text
Timestamp:
Contract:
Baseline:
Commands and observed results:
Changed artifacts:
Decision:
Next state:
```

## State machine

```text
baseline → contract → tracer-bullet → stable-checkpoint → full-gate
→ exact-SHA-review → push → CI → live-workflow → Wiki verification
```

No later state may be claimed without recorded evidence. A failed helper/tool launch is recorded as a tool preflight failure, not as a product deployment failure.
