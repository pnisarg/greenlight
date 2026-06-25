"""Shared helpers: subprocess, logging, paths."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
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
    """Run a command, capturing output. Never raises on non-zero unless check=True."""
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
