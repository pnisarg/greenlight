# Executing a roadmap issue with Pi

Roadmap delivery is intentionally stateless across issues. GitHub issues and
merged pull requests are the durable state; there is no long-running controller,
local task database, or automatic cross-task rebasing.

## One issue, one fresh session, one PR

For an issue whose dependencies are merged:

```sh
git switch main
git pull --ff-only origin main
pi --name GL-XXX
```

Give Pi this prompt, replacing the issue URL:

```text
Execute the Greenlight product roadmap issue <ISSUE_URL> end to end.

Read the issue, docs/product-roadmap.md, repository instructions, relevant
source/tests, and linked dependencies before changing code. Treat the issue's
intent, scope, non-goals, acceptance criteria, and validation as authoritative.
Start from current origin/main and create the issue's configured feature branch.

Within this session:
1. Translate acceptance criteria into failing tests or another observable check.
2. Implement the smallest in-scope change using repository conventions.
3. Run focused validation, then build/lint/typecheck/tests for the affected scope.
4. Perform independent adversarial review for correctness/regressions,
   fail-closed/security behavior, compatibility/migration, test quality, and
   simplicity.
5. Apply every in-scope finding worth fixing now and rerun affected checks.
6. Inspect the final diff and commit with a Conventional Commit subject.
7. Push to pnisarg/greenlight and open a draft PR using the issue's configured
   title and a body containing intent, decisions, validation, residual risks,
   issue link, and dependency information.
8. Comment on the issue with the draft PR URL and validation evidence.

Never merge or deploy. Do not weaken Greenlight's fail-closed invariants. If an
unresolved product/architecture decision is not answered by the issue or product
roadmap, make no speculative choice: comment with the exact blocker and stop.
```

The session may use subagents for fresh read-only review, but only one writer
should modify the task branch/worktree.

## Dependency rule

Do not start a dependent issue until its dependency PR is merged. Begin every
issue from the resulting current `origin/main`. Independent issues may run in
parallel only when they have disjoint files and explicit independent ownership.

## Definition of done

A task is ready for human review only when:

- acceptance criteria are implemented;
- behavior changes have tests;
- affected tests, lint, typecheck, and build pass;
- adversarial findings are dispositioned;
- the branch is clean and pushed;
- a correctly titled draft PR exists;
- the issue links to the PR and records validation and residual risks.

A fresh session ends after opening the draft PR. The human reviews and merges;
no agent merges or deploys.

## Failure behavior

If a session fails or stops, the branch and GitHub issue are sufficient for a
new session to recover:

```text
Continue <ISSUE_URL>. Inspect the existing configured branch and draft PR before
changing anything. Preserve completed valid work, reproduce the current blocker,
finish the issue contract, rerun validation/review, and update the same draft PR.
Never merge or deploy.
```

No local lock or hidden state needs repair.
