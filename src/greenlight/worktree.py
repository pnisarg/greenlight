"""Throwaway worktree management for pipeline runs.

The pipeline never touches your working tree. It checks out the pushed branch
into a disposable worktree created off the bare gate repo, does all its work
there (lint fixes, review fixes, verification), and the worktree is removed when
the run ends.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from .util import run


@contextlib.contextmanager
def checkout(bare: str | Path, branch: str, head_sha: str):
    """Yield a fresh worktree dir containing head_sha checked out as branch."""
    base = Path(tempfile.mkdtemp(prefix="greenlight-wt-"))
    wt = base / "wt"
    try:
        # Create a detached-then-named branch worktree at the pushed head.
        run(["git", "worktree", "add", "--force", "-B", branch, str(wt), head_sha],
            cwd=bare, check=True)
        yield str(wt)
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=bare)
        shutil.rmtree(base, ignore_errors=True)
        # Prune any dangling administrative entries.
        run(["git", "worktree", "prune"], cwd=bare)
