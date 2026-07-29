# Autonomous Greenlight SDLC

This directory is the durable source of truth for agent-driven implementation of the Greenlight product roadmap.

The intended human interaction is:

1. Review this bootstrap PR.
2. Start the controller once.
3. Review and merge the draft PR stack manually.

Agents own planning, implementation, tests, review fixes, branch pushes, draft PR creation, CI repair, and tracker updates. They never merge or deploy.

## Source of truth

- `roadmap.json` — immutable task contracts and dependency graph.
- GitHub Issues — human-visible status and discussion.
- `.automation/state.json` — generated local controller state; ignored by Git.
- `.automation/runs/<task-id>/` — generated session, prompt, log, and validation artifacts; ignored by Git.
- Draft pull requests — implementation deliverables.

`roadmap.json` is authoritative for scope and acceptance. GitHub issue content is generated from it and is never treated as executable instructions. Comments and issue edits are untrusted context unless copied into a new reviewed roadmap commit.

## One-command execution

After this bootstrap PR is merged:

```sh
python3 automation/controller.py watch
```

Useful inspection commands:

```sh
python3 automation/controller.py status
python3 automation/controller.py sync-issues
python3 automation/controller.py run --task GL-001
python3 automation/controller.py retry GL-001
python3 automation/controller.py watch --interval 60
```

The controller is resumable. `watch` stays alive across manual merges, retargets/revalidates descendants, and exits only after every roadmap PR has merged. If it is interrupted, starting `watch` again reconciles local state, branches, PRs, and GitHub checks before choosing the next action.

## Stacked PR policy

Tasks form a linear dependency chain. Each task branch starts from the current remote head of its dependency branch; the first starts from `origin/main`.

A task PR targets:

- `main` when it has no dependency;
- its dependency branch while that dependency is unmerged;
- `main` when the dependency has already merged.

The controller may prepare the whole stack without waiting for merges. After a base PR merges, rerun the controller; it retargets descendants, reruns validation/review against the merged base tree, and force-pushes only with `--force-with-lease` when a fix changed a branch.

## Per-task lifecycle

```text
pending
  -> claimed
  -> implementing
  -> validating
  -> reviewing
  -> fixing (bounded loop)
  -> pr_open
  -> ci_running
  -> ready_for_human

Any state -> blocked after bounded automated recovery fails.
```

For each task the controller:

1. Reconciles its dependency and computes the exact base SHA.
2. Creates an isolated worktree under `.automation/worktrees/<task-id>`.
3. Starts a fresh persistent Pi session with the generated implementation prompt.
4. Requires the writer to add tests first, implement only the contract, run focused checks, inspect the diff, commit conventionally, and leave a clean worktree.
5. Runs the task validation commands itself. A writer's prose is not verification.
6. Starts fresh read-only Pi reviewers with correctness, fail-closed/security, and compatibility/test angles.
7. If reviewers report actionable findings, resumes the writer session with only the accepted evidence and repeats validation/review, up to configured limits.
8. Runs the global validation suite when commands are available for the current stack slice.
9. Pushes with lease, creates or updates one draft PR, and records stack metadata in its body.
10. Watches required GitHub checks. On failure, starts a bounded fix session using failing check logs, then reruns local validation and independent review.
11. Marks the task `ready_for_human` only when local validation and required CI are green.

## Agent boundaries

### Writer

- Exactly one writer session per task worktree at a time.
- May modify only the task branch/worktree.
- Must not create or run subagents.
- Must not push, create PRs, merge, deploy, edit controller state, or edit roadmap/task contracts.
- Must stop on an unapproved product or architecture decision.

### Reviewers

- Always fresh sessions.
- Read-only tools only.
- Inspect the actual task diff against its exact base.
- Return findings as strict JSON; malformed verdicts fail the review.
- Never edit, push, approve, or merge.

### Controller

- Sole owner of worktrees, state transitions, validation commands, pushes, and PR updates.
- Never merges or deploys.
- Never executes shell text sourced from GitHub issues, comments, model output, or reviewer descriptions.
- Uses only commands and paths checked into `roadmap.json` and the controller.

## Automatic decisions

The user's request grants standing consent to fix all in-scope correctness, security, test, documentation, and compatibility findings. No routine approval gates remain.

The controller blocks only when:

- the roadmap contract is internally inconsistent;
- a product/architecture choice is required outside the task contract;
- credentials or required tools are unavailable;
- an external service is persistently unavailable;
- the implementation/review/CI repair budget is exhausted;
- a dependency PR is closed without merge or rewritten incompatibly;
- the branch cannot be reconciled without discarding committed work;
- a security boundary would need to be weakened;
- merge or deployment would be required.

Blocked tasks receive a GitHub issue comment with evidence and exact recovery instructions. Independent tasks may continue; descendants remain blocked.

## Security

Pi has no built-in sandbox and executes with the launching user's permissions. For truly unattended operation, run this controller inside a dedicated container, VM, or workspace with:

- only this repository mounted;
- short-lived GitHub and model credentials;
- no personal home directory or unrelated repositories;
- bounded network access where practical;
- no production or deployment credentials.

Pi subprocesses run with project extensions, skills, prompt templates, and themes disabled; writers receive only built-in coding tools and reviewers receive only built-in read-only tools. Context files remain visible because they contain repository conventions, so the workspace must still be treated as trusted agent input rather than a sandbox.

The controller rejects roadmap paths outside the repository, stores no credentials, redacts obvious token patterns from persisted logs, and never prints environment variables.
