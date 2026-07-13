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

It's small and opinionated, driven by [`pi`](https://pi.dev), and built around
two ideas:

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

> Status: experimental (v0.1). The idea is solid but it has not yet gated large
> volumes of real-world PRs. Dogfood it before you rely on it.

## Install (Pi is required)

> [!IMPORTANT]
> **Pi is greenlight's execution runtime, even when you write code in Claude Code
> or Codex.** Greenlight does not delegate its core pipeline to those clients: it
> shells out to `pi` for intent fallback, review, and fixes. A working,
> authenticated `pi` executable on `PATH` is therefore a hard dependency, not an
> optional UI integration.

Prerequisites: `git`, Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and
Node/npm. [`gh`](https://cli.github.com/) is optional and enables PR creation.

### Recommended install

No repository checkout is needed:

```sh
# 1. Install the Pi runtime.
npm install -g --ignore-scripts @earendil-works/pi-coding-agent

# 2. Authenticate Pi once. In the session, run /login and choose your provider,
#    such as Claude Pro/Max or ChatGPT Plus/Pro (Codex), then exit Pi.
pi

# 3. Install both the greenlight CLI and its Pi package.
uv tool install git+https://github.com/pnisarg/greenlight.git
pi install https://github.com/pnisarg/greenlight

# 4. Verify that the CLI can find Pi, then verify Pi's authentication.
greenlight doctor
pi -p "Reply with exactly: pi is ready"
```

Both installs in step 3 are intentional:

- `uv tool install` provides the `greenlight` executable used in any terminal or
  coding agent.
- `pi install` provides Pi's `/skill:greenlight` workflow and live
  `greenlight_run` card.
- The pipeline itself always runs its agents through Pi. Your selected Pi model
  and credentials apply regardless of whether Claude Code, Codex, or Pi invoked
  `greenlight`.

API keys also work instead of `/login`; configure the provider in Pi before the
first greenlight run.

### Install from a development checkout

```sh
git clone https://github.com/pnisarg/greenlight
cd greenlight
uv venv && . .venv/bin/activate
uv pip install -e .
pi install .
greenlight doctor
```

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
extension shows the same card inline when the agent invokes the gate; because
the gate mirrors events to the per-repo stream, `greenlight watch` can also
re-attach to a tool-launched run after its window has closed. If a run is killed
before it finishes (so it never writes a result), watch doesn't hang on it: once
the stream is idle past `--grace` (default 120s) and the gate process is gone,
it reports the run abandoned and exits 3.

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

### Driving it from Pi, Claude Code, or Codex

The repository ships one Agent Skills-compatible workflow in `skill/SKILL.md`.
It makes the coding agent author a complete intent, commit on a feature branch,
and hand the committed change to the greenlight CLI. The outer coding client can
be Pi, Claude Code, or Codex; **the inner greenlight pipeline still runs on Pi**.

#### Pi

The recommended installation above registers the skill and the live tool. In Pi,
run:

```text
/skill:greenlight                 # validate the committed feature branch
/skill:greenlight <task>          # do the task, commit it, then validate it
```

Pi uses the `greenlight_run` tool when available and shows the live pipeline
card.

#### Claude Code

Expose the installed skill to Claude Code once:

```sh
mkdir -p ~/.claude/skills
ln -s ~/.pi/agent/git/github.com/pnisarg/greenlight/skill \
  ~/.claude/skills/greenlight
```

Then start Claude Code in the repository you are changing and invoke:

```text
/greenlight                 # validate committed work
/greenlight <task>          # implement, commit, and validate a task
```

Claude authors the intent and invokes `greenlight`; greenlight launches Pi for
all pipeline agent work. The Pi process uses the provider selected during the
installation `/login`, not Claude Code's current model session.

#### Codex

Expose the same installed skill to Codex once:

```sh
mkdir -p ~/.agents/skills
ln -s ~/.pi/agent/git/github.com/pnisarg/greenlight/skill \
  ~/.agents/skills/greenlight
```

Start Codex in the repository you are changing, use `/skills` to confirm that
`greenlight` is available, then mention it explicitly:

```text
$greenlight Validate my committed feature branch.
$greenlight Implement <task>, commit it, then validate it.
```

Codex authors the intent and invokes `greenlight`; greenlight launches Pi for all
pipeline agent work. The Pi process uses the provider selected during the
installation `/login`, not Codex's current model session.

The symlinks intentionally point at Pi's installed git package, so
`pi update --extensions` updates the shared skill for all three clients. If a
skill destination already exists, remove or rename it before creating the
symlink. For a team-repository install instead, copy `skill/SKILL.md` into both
`.claude/skills/greenlight/` and `.agents/skills/greenlight/` and commit those
copies.

Claude Code and Codex do not render Pi's live tool card. Run `greenlight watch`
in a second terminal when you want the same pipeline progress outside Pi.

### Live pipeline card in Pi

For [`pi`](https://pi.dev), the repo also ships a small package (`package.json`
+ `pi/extensions/greenlight.ts`) that adds a `greenlight_run` tool. When the
coding agent invokes it, the tool-call card becomes a **live diagram** of the
pipeline as it advances — intent → lint → review loop (per reviewer, per round)
→ verify → PR — so you can watch the handoff between the agent and the gate in
real time. The agent’s architecture is unchanged: it still authors the intent
and hands off; the extension only renders greenlight’s event stream.

```sh
# Already included in the recommended installation above.
pi install https://github.com/pnisarg/greenlight
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
# model = "anthropic/claude-sonnet-4"     # pi model for all steps; empty = pi default
# review_model = "openai-codex/gpt-5.5:high"  # default model for all reviewers (":high" = reasoning effort)
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
# Reviewers run in parallel (each is its own read-only pi process), so a round
# costs the slowest reviewer's wall time, not the sum.

[[reviewers]]
name = "security"
focus = "Injection, auth gaps, secret leakage, unsafe deserialization, SSRF, path traversal, missing validation."
# Per-reviewer model. Precedence: this `model` > `review_model` > `model` > pi
# default — so you can run security on GPT-5.5 while the coding/fix agent stays
# on Claude Opus. Omit to inherit the run's model.
# model = "openai-codex/gpt-5.5:high"

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

[ci]
# Post-PR CI monitoring. After the PR opens, poll its checks, auto-fix failures
# (intent-preserving) up to max_fix_rounds, and only report green once the real
# remote CI is green. The authoritative test signal when tests need deps or
# services the throwaway worktree can't provide. Requires `gh`; GitHub only.
enabled = false
# provider = "github"
# timeout = 2700                         # idle seconds before giving up (0 = forever)
# max_fix_rounds = 2                     # intent-preserving fix attempts on failure
# required_checks = []                   # gate only on these check names; empty = all

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
| **review loop** | N configurable read-only reviewers (run in parallel) → intent-preserving fix → re-review, up to `max_review_rounds`. | no blocking findings remain |
| **verify** | Backend tests and/or frontend screenshot based on diff classification; evidence committed. | tests pass / server boots |
| **PR** | Composes intent + evidence into the PR body, opens via `gh`. Idempotent. | — |
| **ci** (opt-in) | Polls the PR's real CI checks; on failure pulls `gh run --log-failed`, applies an intent-preserving fix, re-pushes, re-polls (≤ `max_fix_rounds`). | CI green |

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
  steps/        intent, lint, review, verify, pr, ci

pi/extensions/
  pipeline-state.ts   pure reducer + card renderer over the event stream
  greenlight.ts       greenlight_run tool: spawns the gate, tails events,
                      streams the live card (TUI glue only)
```

The gate hook is a tiny shim that calls back into `greenlight hook`, which holds
all the logic — so the installed hook stays stable across upgrades. On hook
entry leaked `GIT_*` env vars are scrubbed so worktree git commands honor their
cwd.
