"""Shared helpers: subprocess, logging, paths."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ANSI is cheap and these messages also flow over `git push` as `remote:` lines.
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _color(s: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stderr.isatty():
        # Inside a post-receive hook stderr is a pipe, so default to no color
        # unless explicitly forced.
        if os.environ.get("GREENLIGHT_FORCE_COLOR") != "1":
            return s
    return f"{code}{s}{_RESET}"


def step(msg: str) -> None:
    print(_color(f"=> {msg}", _BOLD), file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(_color(f"   {msg}", _DIM), file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(_color(f" ok  {msg}", _GREEN), file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(_color(f" !!  {msg}", _YELLOW), file=sys.stderr, flush=True)


def fail(msg: str) -> None:
    print(_color(f" xx  {msg}", _RED), file=sys.stderr, flush=True)


class GreenlightError(Exception):
    """User-facing error that should abort the run with a clean message."""


class Deadline:
    """A wall-clock budget for an entire run.

    The root cause of "stuck for hours": every step times out individually, so a
    degraded LLM gateway lets intent + lint + (reviewers x rounds) + verify each
    crawl to its own per-call ceiling, stacking to multi-hour runs. A single
    Deadline shared across the run fixes that: `clamp()` shrinks every per-call
    timeout to the time left, so no call outlives the budget, and `expired()`
    lets the pipeline stop gracefully at the next gate instead of grinding on.

    budget_seconds <= 0 or None disables the cap (clamp/expired become no-ops),
    preserving the old unbounded behavior for callers that opt out.
    """

    def __init__(self, budget_seconds: float | None):
        self._end = (
            time.monotonic() + budget_seconds
            if budget_seconds and budget_seconds > 0
            else None
        )

    def remaining(self) -> float | None:
        """Seconds left, or None when uncapped. Never negative."""
        if self._end is None:
            return None
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def clamp(self, timeout: float | None) -> float | None:
        """Shrink a per-call timeout to fit the remaining budget.

        Returns a small positive floor (not 0) when the budget is nearly spent,
        so the clamped call fails fast on its own timeout rather than blocking
        forever on a 0/None timeout.
        """
        rem = self.remaining()
        if rem is None:
            return timeout
        floored = max(rem, 0.1)
        return floored if timeout is None else min(timeout, floored)


@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    args: list[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    input_text: str | None = None,
    timeout: float | None = None,
) -> Run:
    """Run a command, capturing output. Never raises on non-zero unless check=True.

    A timeout is turned into a normal non-zero Run (exit 124, the conventional
    timeout code) rather than a raised TimeoutExpired: a single slow subprocess
    (a hung reviewer agent, a stuck test command) must not crash the whole gate
    with a traceback. check=True still surfaces it as a GreenlightError.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        r = Run(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.output if isinstance(e.output, str) else (e.output or b"").decode(errors="replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(errors="replace")
        note = f"timed out after {timeout}s"
        r = Run(124, out or "", f"{err}\n{note}".strip() if err else note)
    if check and not r.ok:
        raise GreenlightError(
            f"command failed ({r.code}): {' '.join(args)}\n{r.err.strip() or r.out.strip()}"
        )
    return r


def which(name: str) -> str | None:
    return shutil.which(name)


def repo_id(abs_path: str) -> str:
    """Deterministic 12-char id from an absolute path."""
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]


def state_dir() -> Path:
    """Where gate bare repos and run logs live."""
    override = os.environ.get("GREENLIGHT_HOME")
    if override:
        return Path(override)
    return Path.home() / ".greenlight"
