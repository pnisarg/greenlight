# Contributing to greenlight

Thanks for your interest. greenlight is an experimental project; contributions,
issues, and ideas are welcome.

## Development setup

```sh
git clone https://github.com/pnisarg/greenlight
cd greenlight
uv venv && . .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -q
```

You need `git` and [`pi`](https://pi.dev) on your PATH. `gh` is optional (it
enables PR creation).

## Ground rules

- **Keep it lean.** No daemon, no database, no heavy dependencies. The runtime
  is intentionally stdlib-only; agent work is delegated to `pi`. New runtime
  dependencies need a strong justification.
- **Tests are required** for behavior changes. The suite uses a fake `pi` shim
  (`tests/fake_pi.py`) so it runs offline with no API spend. Add unit tests for
  pure logic and exercise pipeline behavior through the e2e harness where it
  matters.
- **Commits follow [Conventional Commits](https://www.conventionalcommits.org/)**:
  `type(scope): summary` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`,
  `ci`, etc.). PR titles follow the same convention.
- **Match the existing style.** Minimal, precise changes; comment only
  non-obvious "why"; don't refactor unrelated code in the same PR.

## Before opening a PR

```sh
python -m pytest -q          # all tests pass
```

Open the PR against `main`. Describe the intent of the change and how you
verified it. Small, reversible PRs get reviewed faster.

## Reporting bugs

Open an issue with what you ran, what you expected, and what happened (include
the `greenlight` output and `greenlight doctor`). For anything security-related,
see [SECURITY.md](SECURITY.md).
