# greenlight

> Nothing ships until it gets the green light.

`greenlight` puts a local git gate in front of your real remote. Push to
`greenlight` instead of `origin` (or run `greenlight run`), and your change goes
through an AI-driven pipeline in a throwaway worktree — and only reaches the real
remote once every gate is green.

```
        your feature branch
              │  git push greenlight   (or: greenlight run)
              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  throwaway worktree — your working tree is never touched    │
   │  intent → format/lint → review loop → verify → PR           │
   └──────────────────────────────────────────────────────────┘
              │  every gate green
              ▼
        clean PR on your real remote
```

It's built small and opinionated — ~2.2k lines of stdlib Python, driven by
[`pi`](https://pi.dev) — around two ideas:

1. **A real review loop you configure.** Multiple reviewers (brutal code review,
   security, your own focus or a pi skill) each run independently and read-only;
   blocking findings get an **intent-preserving** fix, then everyone re-reviews.
   Repeat until clean. The fix step is forbidden from changing your intent.
2. **Change-aware verification.** The diff is classified — backend changes run
   tests, frontend changes capture a screenshot, mixed does both — and the
   evidence is committed so it shows up on the PR.

Intent is authored by the coding agent that made the change and passed at
handoff (`--intent` / `--intent-file -`), not scraped from agent transcripts, so
it's fast and accurate and becomes frozen ground truth for the review loop.

## Design

greenlight deliberately stays lean: no daemon, no database, no TUI, and no
built-in agent adapters. The whole thing is stdlib Python that shells out to
`pi` for every agent task (intent, review, fixes), with a plain-Python
orchestrator holding all control flow and state so the pipeline is deterministic
and debuggable. The one piece of infrastructure is the gate itself: a bare repo
plus a `post-receive` hook that intercepts your push, runs the pipeline in a
throwaway worktree, and forwards to the real remote only when every gate is
green.

> Status: experimental (v0.1). Built fast; the idea is solid but it has not yet
> gated large volumes of real-world PRs. Dogfood it before you rely on it.

## Install

```sh
git clone https://github.com/pnisarg/greenlight
cd greenlight
uv venv && . .venv/bin/activate
uv pip install -e .
```

Requires `git` and `pi` on PATH. `gh` is optional (enables PR creation).

## Use

```sh
greenlight init                  # set up the gate + write a starter .greenlight.toml
git checkout -b feat/my-change   # work on a feature branch, commit your work

# either push through the gate…
git push -o intent="add a greeting helper" greenlight feat/my-change
# …or run it explicitly
greenlight run --intent "add a greeting helper"
```

On pass, the branch is forwarded to your push target and a PR is opened with the
intent and verification evidence in the body.

### Watch it run

The `git push greenlight` path runs the pipeline server-side (in the gate's
post-receive hook), so there's nothing to watch in your terminal by default. Run
`greenlight watch` in a second terminal to render the live pipeline card from the
event stream:

```sh
greenlight watch            # idles until a run starts, then follows it live
greenlight watch --once     # print the latest run's card and exit
```

It reads the per-repo event stream the gate publishes (or `$GREENLIGHT_EVENTS`
if set), and exits 0/1 mirroring the run's pass/fail — handy in scripts. The pi
extension shows the same card inline when the agent invokes the gate.

### Inspect what the reviewers found

The PR intentionally carries no review findings — most of the time you only care
that the gate went green. For the times you do want to see what the reviewers
flagged, every run's findings are archived locally and printed on demand:

```sh
greenlight review-log           # detailed per-round findings for the latest run
greenlight review-log --list    # enumerate retained runs (newest first)
greenlight review-log --run 2   # show a specific past run (newest = 1)
```

Unlike the `watch` card (a compact status view), this lists every finding per
round and reviewer — severity, file:line, whether it blocked, and the
description. The data comes from the per-repo event stream the gate already
writes; greenlight archives each run under `~/.greenlight/runs/<id>/history/`
before the next run truncates the live stream (last 25 runs kept), so nothing
lands on the branch or PR.

### Disk hygiene

Each run checks out into a **throwaway** worktree that is removed when the run
ends (pass or fail), so per-branch worktrees never accumulate. If a run is
hard-killed (SIGKILL/OOM) before its cleanup runs, the next run sweeps any
orphaned `greenlight-wt-*` temp dirs and prunes their git admin entries — the
gate self-heals without a daemon.

The longer-lived disk cost is the per-repo bare gate repo under
`~/.greenlight/repos/<id>.git` (a mirror of the upstream, created once at
`greenlight init`). Its object store grows as you push branches through the
gate. Repack it on demand:

```sh
greenlight gc          # gc the gate repo for the current repo
greenlight gc --all    # gc every provisioned gate repo
```

### Driving it from an agent

`greenlight init` is meant to be paired with the `/greenlight` skill
(`skill/SKILL.md`). Point your agent at it; it runs the pipeline, reports which
gate passed/failed, and (task-first mode) does the work before validating.

### Live pipeline card in pi

For [`pi`](https://pi.dev), the repo also ships a small package (`package.json`
+ `pi/extensions/greenlight.ts`) that adds a `greenlight_run` tool. When the
coding agent invokes it, the tool-call card becomes a **live diagram** of the
pipeline as it advances — intent → lint → review loop (per reviewer, per round)
→ verify → PR — so you can watch the handoff between the agent and the gate in
real time. The agent’s architecture is unchanged: it still authors the intent
and hands off; the extension only renders greenlight’s event stream.

```sh
pi install ./greenlight        # installs the extension + the /greenlight skill
```

The extension is a pure renderer over a machine-readable event stream that
greenlight emits as JSONL to `$GREENLIGHT_EVENTS` (additive; unset = no-op, so
the gate behaves identically when nothing is watching). That stream is the
handoff contract — any other UI can consume it the same way.

## Configure — `.greenlight.toml`

```toml
[greenlight]
max_review_rounds = 3
push_target = "origin"
# model = "anthropic/claude-sonnet-4"     # pi model; empty = pi default
evidence_dir = ".greenlight/evidence"

[checks]
# format_cmd = "ruff format ."
# lint_cmd = "ruff check ."

# Define exactly what review cares about. Each reviewer is an independent,
# read-only agent. Use a `focus` prompt or load a pi `skill`.
[[reviewers]]
name = "brutal"
focus = "Brutally honest senior review. Real bugs, broken edge cases, race conditions, bad error handling, needless complexity. No style nits."
blocking_severity = "warning"   # error | warning | info

[[reviewers]]
name = "security"
focus = "Injection, auth gaps, secret leakage, unsafe deserialization, SSRF, path traversal, missing validation."

# [[reviewers]]
# name = "house-style"
# skill = "/path/to/review-skill"        # load a pi skill instead of a prompt

[verify]
# Backend tests (auto-detected from project files if omitted).
# [[verify.backend]]
# name = "unit"
# cmd  = "uv run pytest -q"

[verify.frontend]
url = "http://localhost:3000"
# server_cmd = "npm run dev"             # booted to capture a screenshot
ready_path = "/"

[routing]
# Override the file globs that classify a change as frontend/backend.
# frontend = ["*.tsx", "frontend/*"]
# backend  = ["*.py", "backend/*"]
```

## Pipeline

| Step | What it does | Gate |
|------|--------------|------|
| **intent** | Uses supplied intent, else summarizes the diff once. Treated as ground truth. | — |
| **format/lint** | Runs configured format/lint; commits formatter output; bounded non-functional auto-fix for lint errors. | lint must pass |
| **review loop** | N configurable read-only reviewers → intent-preserving fix → re-review, up to `max_review_rounds`. | no blocking findings remain |
| **verify** | Backend tests and/or frontend screenshot based on diff classification; evidence committed. | tests pass / server boots |
| **PR** | Composes intent + evidence into the PR body, opens via `gh`. Idempotent. | — |

## Development

```sh
. .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -q
```

## Architecture

```
src/greenlight/
  cli.py        init | run | watch | review-log | gc | hook | doctor
  gate.py       bare repo + greenlight remote + post-receive hook + gc
  worktree.py   throwaway worktree per run (+ stale-orphan sweep)
  agent.py      pi -p --mode json runner + output parsing
  config.py     .greenlight.toml schema, default reviewers
  diff.py       FE/BE/mixed classification
  pipeline.py   orchestration
  events.py     structured JSONL event stream (the UI handoff contract)
  render.py     reducer + card renderer for `greenlight watch`
  gitx.py       git command helpers
  util.py       process runner, logging, state paths
  steps/        intent, lint, review, verify, pr

pi/extensions/
  pipeline-state.ts   pure reducer + card renderer over the event stream
  greenlight.ts       greenlight_run tool: spawns the gate, tails events,
                      streams the live card (TUI glue only)
```

The gate hook is a tiny shim that calls back into `greenlight hook`, which holds
all the logic — so the installed hook stays stable across upgrades. On hook
entry leaked `GIT_*` env vars are scrubbed so worktree git commands honor their
cwd.
