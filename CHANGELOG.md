# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
