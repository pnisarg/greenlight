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
import time
from pathlib import Path

from .util import info, run

_TMP_PREFIX = "greenlight-wt-"
# A run never lasts hours; anything older was orphaned by a hard-killed process
# (SIGKILL/OOM) whose cleanup `finally` never ran. Wide margin avoids racing a
# concurrent live run sharing the same temp root.
_STALE_AGE_SECONDS = 6 * 3600


def sweep_stale(bare: str | Path, tmp_root: str | Path | None = None) -> None:
    """Reclaim worktree temp dirs orphaned by hard-killed runs (best-effort).

    The normal teardown in `checkout` handles clean exits; this catches the
    SIGKILL case where the dir (and its git admin entry) leaked. Runs on each
    new checkout so the gate self-heals without a daemon.
    """
    root = Path(tmp_root) if tmp_root else Path(tempfile.gettempdir())
    cutoff = time.time() - _STALE_AGE_SECONDS
    removed = 0
    for d in root.glob(f"{_TMP_PREFIX}*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    # Drop git admin entries for worktrees whose dirs are now gone.
    run(["git", "worktree", "prune"], cwd=bare)
    if removed:
        info(f"swept {removed} orphaned worktree dir(s)")


@contextlib.contextmanager
def checkout(bare: str | Path, branch: str, head_sha: str):
    """Yield a fresh worktree dir containing head_sha checked out as branch."""
    sweep_stale(bare)
    base = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX))
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
