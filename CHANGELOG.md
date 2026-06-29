# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- The review gate no longer fails **open** on an inconclusive reviewer. A
  reviewer whose pi call raised (timeout/crash) or whose output carried no
  `findings` list (prose, truncated JSON, a degraded gateway) was parsed as
  "0 findings → clean" — a false green light, the worst failure for a gate. It
  was also inconsistent: a timeout with no text crashed the whole gate while a
  timeout with partial text silently passed. The reviewer is now retried once to
  absorb a transient blip; if it still yields no usable verdict the review fails
  **closed** with a synthesized blocking finding (visible on the card and in
  `greenlight review-log`), and the fix loop is skipped since a flaky reviewer
  isn't a code defect the fix agent can repair.

### Added
- `run_timeout` config (default 1200s): a single wall-clock budget for the whole
  run. A shared deadline clamps every agent/subprocess call to the time
  remaining and aborts the pipeline gracefully at the next gate once exhausted.
  Fixes runs grinding for hours on a degraded LLM gateway, where each step
  previously timed out individually and the per-step ceilings stacked (intent +
  lint + reviewers x rounds + verify) to ~4.5h. `0` disables the cap.

### Fixed
- The pi extension no longer leaks a `/tmp/greenlight-events-*` dir per run.
  Since the gate is spawned detached (to survive the window closing), the tool's
  own cleanup is skipped on that path; each new run now sweeps stale events dirs
  left by hard-killed runs, mirroring the worktree self-heal. Best-effort and
  age-gated (6h) so it never races a concurrent live run.
- A run launched from the pi `greenlight_run` tool no longer dies when the pi
  window closes: the extension spawns the gate detached (its own process group)
  with stdio redirected to a file instead of pipes back to pi, so a parent
  SIGHUP and the subsequent broken-pipe-on-stderr can't tear the run down
  mid-review. The gate also installs SIGTERM/SIGHUP handlers so any termination
  unwinds the worktree cleanup (no more orphaned `greenlight-wt-*` dirs).
- `greenlight watch` no longer spins forever on a run that was killed before it
  finished. `run_start` now stamps the gate PID; once the event stream is idle
  past `--grace` (default 120s) and that PID is gone, watch reports the run
  abandoned and exits non-zero (3) instead of polling a dead stream.

### Added
- `greenlight review-log`: inspect the reviewer findings from a past run
  (detailed per-round, per-reviewer breakdown). `--list` enumerates retained
  runs, `--run N` selects one (newest = 1). Each run's event stream is archived
  under `~/.greenlight/runs/<id>/history/` before the next run truncates the
  live stream (last 25 kept), so findings stay inspectable without ever landing
  on the branch or PR.
- `greenlight gc [--all]`: repack the per-repo bare gate repos to reclaim disk.
  Reports on-disk (block-level) size before/after. Uses git's default prune
  grace period (not `--prune=now`) so it stays safe to run while a push/fetch is
  writing objects into the same daemonless bare repo.
- Self-healing worktree cleanup: each run sweeps orphaned `greenlight-wt-*` temp
  dirs left by hard-killed runs (SIGKILL/OOM) and prunes their git admin
  entries, on top of the existing per-run teardown.

## [0.1.0] - 2026-06-24

Initial experimental release.

### Added
- Local git gate: a bare-repo `greenlight` remote plus a `post-receive` hook
  that intercepts a push, runs the pipeline in a throwaway worktree, and
  forwards to the real remote only on pass.
- Pipeline: intent capture → format/lint → configurable multi-reviewer review
  loop (intent-preserving fix between rounds) → change-aware verify (backend
  tests or frontend screenshot) → PR with intent + evidence.
- Agent-authored intent via `--intent` / `--intent-file -`; degraded
  diff-reconstruction fallback when none is supplied.
- Configurable reviewers in `.greenlight.toml` (focus prompt or pi skill),
  shipping with `brutal` and `security` defaults.
- Change classification (frontend / backend / mixed) from the diff.
- `/greenlight` agent skill.
- CLI: `init`, `run`, `hook`, `doctor`.

[Unreleased]: https://github.com/pnisarg/greenlight/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pnisarg/greenlight/releases/tag/v0.1.0
