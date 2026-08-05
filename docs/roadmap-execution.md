# Executing a roadmap issue with Pi

Roadmap delivery is intentionally stateless across issues. GitHub issues and
merged pull requests are the durable state; there is no long-running controller,
local task database, or automatic cross-task rebasing.

## The runner

`automation/run_roadmap.py` automates one issue per invocation and then stops.
It holds no state of its own: it derives everything from GitHub and git, so an
interrupted run leaves nothing to repair.

```sh
python3 automation/run_roadmap.py status       # every issue and what it waits on
python3 automation/run_roadmap.py next         # the next ready issue URL
python3 automation/run_roadmap.py run          # implement it, open a draft PR, stop
python3 automation/run_roadmap.py run 23       # a specific ready issue
python3 automation/run_roadmap.py run --dry-run # show the plan and prompt only
python3 automation/run_roadmap.py sync-labels  # reconcile status:ready
```

`run` refuses to start an issue whose dependency is still open, fast-forwards
`main` (never merges anything else), hands the issue to a fresh `pi` session with
the prompt below, then **verifies the completion contract from GitHub rather than
from the session's own summary**: the branch must be pushed, exactly one open
pull request must exist for it, that PR must be a draft, its title must match the
issue's configured title, and its body must close the issue. If any of that is
missing the issue is labelled `status:blocked` with the specific unmet
requirements, and the run exits non-zero instead of looking successful.

The runner itself cannot merge, push, publish a release, or rewrite history:
those commands are refused before they reach a subprocess, and the only write it
makes to the base branch is a fast-forward. The pi session it starts does push
its own feature branch — that is the session's job — but nothing in the loop
merges, so a human merge stays the only way work advances. After merging, run it
again for the next issue.

Session output is streamed to your terminal and copied, redacted, to
`.git/roadmap-runs/<issue>-<timestamp>.log`. Those logs are evidence, never
inputs; deleting them breaks nothing.

## One issue, one fresh session, one PR

To drive a session by hand instead:

For an issue whose dependencies are merged:

```sh
git switch main
git pull --ff-only origin main
pi --name GL-XXX
```

Give Pi this prompt, replacing the issue URL. `FRESH_PROMPT` in
`automation/run_roadmap.py` is the executable copy of it; keep the two in sync.

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

The last three are what the runner verifies mechanically after a session exits.
The first four are the session's own responsibility; the runner cannot judge
them, which is why the PR is a draft and a human still reads it.

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

`RECOVERY_PROMPT` in `automation/run_roadmap.py` is the executable copy. The
runner sends it automatically whenever the issue's configured branch already
exists locally or on the remote, so rerunning `run` after a failure resumes
rather than restarts.

Clear `status:blocked` once the blocker is resolved; the runner will not pick up
a blocked issue. No local lock or hidden state needs repair.
