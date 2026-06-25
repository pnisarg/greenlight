# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
