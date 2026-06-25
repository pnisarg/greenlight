---
name: greenlight
description: Validate code changes through the greenlight gate — intent capture, format/lint, a configurable multi-reviewer review loop, change-aware verification (backend tests or frontend screenshot), and a clean PR — before they reach the real remote. Use when the user asks to run greenlight, gate/ship/validate changes, push safely, or invokes /greenlight.
user-invocable: true
---

# greenlight

`greenlight` is a local git gate: nothing ships until it gets the green light.
A change runs through **intent → format/lint → review loop → verify → PR** in a
throwaway worktree, and only forwards to the real remote when every gate passes.

You drive it through the `greenlight` CLI. Output goes to stderr as readable
`=> step` / ` ok ` / ` !! ` / ` xx ` lines; the exit code is the source of truth
(0 = passed, non-zero = failed).

## Prefer the `greenlight_run` tool when it exists

If the `greenlight_run` tool is available (the greenlight pi package is
installed), call it instead of shelling out to the CLI. It runs the same
pipeline but renders a **live pipeline card** (intent → lint → review loop →
verify → PR) as it progresses, so the user sees the handoff between you and the
gate in real time. Pass your authored intent as the `intent` argument. The work
must already be committed on a feature branch. Everything below about authoring
intent, the review loop, and reporting still applies — the tool just replaces
the `greenlight run` invocation and visualizes it. Fall back to the CLI only
when the tool is not registered.

## You are the source of intent

greenlight's review loop is only as good as the intent it's given. The agent
that made the change (you) is the *only* thing that knows the **why** — what the
user asked for, the decisions and tradeoffs you made, what you deliberately did
or didn't do. The diff shows *what* changed but never *why*. So **authoring the
intent is your job**, and it is the single most important input you provide.

greenlight will not scrape it from transcripts. If you don't supply it, it falls
back to reconstructing intent from the diff — a degraded path that can't recover
your deliberate choices, so reviewers flag things the user already decided and
the fix loop wastes rounds. Never rely on the fallback when you know the intent.

## Two ways to invoke

- **Validate-only** — bare `/greenlight`. The user's work is already committed on
  a feature branch; validate it. You may not have made the change, so write the
  best intent you can from the conversation and the diff.
- **Task-first** — `/greenlight <task>`. First do the task yourself, commit it on
  a **feature branch** (never the default branch), then validate. You just made
  the change, so you have the richest possible intent — write it down in full.

## Before you start

- Work must be **committed** on a **feature branch** (not main/master).
- The repo must be initialized: `greenlight init` (idempotent; also writes a
  starter `.greenlight.toml`).
- `pi` must be on PATH (greenlight drives it for review/intent/fixes). `gh` is
  optional — without it the branch is still validated and forwarded, but the PR
  is not opened.

## Authoring the intent

Before you run, write a few sentences to a short paragraph. Err on completeness,
not brevity — a thin one-liner makes the reviewers noisy. Cover:

- **Goal**: what the user set out to accomplish, in their terms (not a diff
  summary).
- **Decisions & tradeoffs**: choices you made that a reviewer reading only the
  diff would not understand or might mistake for a bug.
- **Deliberately ruled in/out**: anything you intentionally did, skipped, or
  deferred — especially deletions, simplifications, or "surprising" changes.
- **Constraints**: anything the user explicitly asked for or forbade.

The review loop uses this verbatim as ground truth to tell a deliberate choice
apart from a defect, and it becomes the PR's Intent section.

## Running it

Intent is usually multi-line, so pass it on **stdin** to avoid shell-escaping
pain (preferred):

```sh
greenlight run --intent-file - <<'INTENT'
Add a per-org rate limiter to the public API.
Chose a token-bucket in Redis over in-process counters so limits hold across
replicas; accepted the extra Redis round-trip on the hot path as the tradeoff.
Deliberately did NOT rate-limit internal service-to-service calls (out of scope
per the ticket). Removed the old IP-based limiter on purpose; it's superseded.
INTENT
```

For a short, single-line intent, `--intent "..."` is fine. You can also write the
intent to a file and pass `--intent-file path`.

`greenlight run` validates the current branch in a worktree and, on pass,
forwards it to the configured push target and opens a PR. It blocks while
reviewers, the fix loop, and verification run (each can take minutes) — allow a
long timeout; do not cancel because it seems slow.

A human can also `git push greenlight <branch>` directly; the gate runs the same
pipeline. Intent can ride along as a push option
(`git push -o intent="..." greenlight <branch>`), but push options are size-
limited and single-line — prefer `greenlight run --intent-file -` whenever you
have real intent to convey.

## The review loop

Each reviewer in `.greenlight.toml` (e.g. `brutal`, `security`, or your own
focus/skill) runs as an independent **read-only** agent. Blocking findings are
fixed by a single intent-preserving fix agent, then every reviewer re-runs.
Repeat until clean or `max_review_rounds`. The fix agent must never change the
intent — it fixes forward, never deletes the author's deliberate code to silence
a finding.

To change what review cares about, edit the `[[reviewers]]` entries — add a
`focus` prompt or point `skill` at a pi skill (e.g. a brutal-code-review skill).

## Verification is change-aware

greenlight classifies the diff and routes verification:
- **backend** → runs configured (or auto-detected) test commands; logs are saved
  as evidence.
- **frontend** → boots the configured dev server and captures a screenshot.
- **mixed** → both.

Evidence is committed under the evidence dir so it travels with the branch and
renders on the PR.

## Reporting the outcome

On exit 0: tell the user what was validated — the classification, reviewer
findings (and any fixes applied), verification evidence (test result / screenshot
path), and the PR link if one was opened. If the trace shows intent was
"reconstructed from the diff", say so — it means intent wasn't supplied and the
review ran on a weaker signal; offer to re-run with proper intent.

On non-zero: read the stderr trace, report which gate failed and why (a blocking
finding the fix loop couldn't resolve, a failing test, lint). Fix it, commit on
the same branch, and re-run — don't leave the user at a failure without a next
step.

## Inspecting / config

```sh
greenlight doctor    # check git/pi/gh availability
greenlight init      # (re)initialize the gate; idempotent
```

Config lives in `.greenlight.toml` (reviewers, verify commands, FE/BE routing,
format/lint commands, evidence dir, model, push target).
