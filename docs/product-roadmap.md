# Greenlight product roadmap

This document captures the product direction selected by the design council and
provides the shared context for implementation issues. It is a plan, not an
executable workflow or background controller.

## Product position

Greenlight is the control tower between coding agents and remote Git: a
local-first, cloud-ready product that lets a developer delegate implementation,
inspect an independently governed gate, choose remediation, and deliberately
forward an exact reviewed candidate.

It is not a browser IDE. The product owns the handoff, review decisions,
pipeline visibility, and shipment confidence. Editing, arbitrary terminals, and
file exploration stay in the developer's existing tools.

## Product journey

1. Register and trust a local repository.
2. Give Pi a coding prompt and observe summarized agent/tool activity.
3. Pi stops at a committed handoff with an authored intent.
4. Review the exact commit, diff summary, intent, and Greenlight policy.
5. Deliberately start Greenlight.
6. Watch every stage and reviewer transition.
7. Select findings to remediate; users cannot waive blocking policy.
8. Re-review every changed candidate with all configured reviewers.
9. Inspect the final delta and authorize the exact final SHA.
10. Greenlight forwards with compare-and-swap semantics and reports PR/CI
    delivery separately from local gate truth.

## Product invariants

- Greenlight's Python orchestrator remains the sole pass/fail authority.
- Reviewers are read-only; one fix agent performs selected remediation.
- Missing, malformed, inconclusive, reproduced, new, or unselected blocking
  findings prevent forwarding.
- A blocker cannot be dismissed, downgraded, accepted as risk, or cleared by
  repeatedly rerunning an unchanged commit.
- Intent is editable before a run; changing it afterward creates a successor
  run and invalidates prior findings.
- Final authorization is bound to the exact candidate, intent, policy, and
  verification basis.
- Browser clients receive opaque repository/run IDs, never arbitrary host paths
  or executable commands.
- Pi runs with local-user authority; hosted execution requires isolated runners,
  tenant identity, authorization, and secret controls.

## Delivery phases

### Phase 1: integrity foundation

Before relying on a richer UI, make the gate's record and forwarding semantics
safe for interaction:

- explicit required verification;
- truthful gate/forward/PR/post-forward-CI outcomes;
- versioned per-run event journals with stable run, event, reviewer, and finding
  identities;
- isolated run refs and expected-head forwarding;
- external bounded/redacted verification evidence.

Strict malformed-verdict handling was delivered in PR #39.

### Phase 2: read-only Gate Console

Build a loopback-only service and focused React UI:

- registered repositories and health;
- exact-SHA handoff and intent;
- replayable SSE run timeline;
- complete round/finding history;
- evidence and bounded diagnostics;
- accurate passed, failed, cancelled, abandoned, forwarded, PR-failed, and
  post-forward-CI states.

### Phase 3: safe intervention

- pause after a complete blocking review round;
- accept fix-selected, fix-all, or stop through a separate authenticated control
  channel;
- reject stale commands by run and review basis;
- re-review all reviewers after every changed HEAD;
- require exact final-candidate authorization before forwarding.

### Phase 4: Pi Workbench

Use Pi's `AgentSessionRuntime` SDK behind a supervised local worker:

- persistent coding sessions;
- prompt, steer, follow-up, and abort;
- conversation and collapsed tool activity;
- changed-file, test, and commit summaries;
- agent-authored intent and deliberate transition into the Gate Console.

Do not add Monaco, an embedded terminal, a file explorer, a model marketplace,
or cloud collaboration in this phase.

### Phase 5: product hardening

Add realistic browser smoke tests, crash/reconnect/cancellation coverage,
accessibility validation, security/retention/redaction review, installation and
migration guidance, screenshots, and changelog entries.

## Implementation issues

| Phase | Issues |
| --- | --- |
| Integrity foundation | #22–#27 |
| Read-only Gate Console | #28–#31 |
| Safe intervention | #32–#34 |
| Pi Workbench | #35–#36 |
| Product hardening | #37 |

Strict malformed-verdict handling was completed by #21 / PR #39. The remaining
issues are implementation contracts, not an automatically executed queue. An
issue becomes ready only after its listed dependency PR merges.

## How roadmap issues are executed

GitHub is the durable tracker. Each implementation issue is a self-contained
contract with intent, scope, non-goals, acceptance criteria, dependencies, and
validation. A fresh Pi session implements one issue from current `origin/main`,
runs tests and independent review, opens a draft PR, then stops. The human
reviews and merges before a dependent issue starts.

See `docs/roadmap-execution.md` for the repeatable session prompt and completion
contract.
