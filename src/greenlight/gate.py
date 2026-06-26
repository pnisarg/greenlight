"""The git gate: intercept `git push greenlight` and run the pipeline.

The core trick, trimmed to the essentials:
  * a bare repo under ~/.greenlight/repos/<id>.git acts as the push target
  * a `greenlight` remote on your repo points at that bare repo
  * a post-receive hook fires when you push, runs the pipeline in a throwaway
    worktree, and forwards the branch to the real remote only on pass.

No daemon, no DB. The hook runs synchronously and streams progress back to your
terminal as `remote:` lines during the push.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import gitx
from .util import GreenlightError, ok, run, state_dir

REMOTE_NAME = "greenlight"

# The hook is a tiny shim: it reads pushed refs on stdin and calls back into the
# greenlight CLI, which holds all the logic. Keeps the hook itself stable.
_HOOK_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail
# Installed by greenlight. Forwards pushed refs to the pipeline.
exec {python} -m greenlight hook --bare "{bare}" --work "{work}"
"""


def repos_dir() -> Path:
    return state_dir() / "repos"


def bare_path(repo_id: str) -> Path:
    return repos_dir() / f"{repo_id}.git"


def list_bare_repos() -> list[Path]:
    """All provisioned bare gate repos under the state dir."""
    d = repos_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.git") if (p / "HEAD").exists())


def init(work_dir: str, push_target: str = "origin") -> dict:
    """Create or repair the gate for the repo at work_dir. Idempotent."""
    root = gitx.main_repo_root(work_dir)
    origin = gitx.remote_url(root, push_target)
    if not origin:
        raise GreenlightError(
            f"no '{push_target}' remote on {root}; add it before greenlight init"
        )

    rid = _repo_id(root)
    bare = bare_path(rid)
    bare.parent.mkdir(parents=True, exist_ok=True)

    _provision_bare(bare, origin)
    _install_hook(bare, root)
    _ensure_remote(root, bare)

    ok(f"gate ready  {REMOTE_NAME} -> {bare}")
    return {
        "repo_root": root,
        "bare": str(bare),
        "remote": REMOTE_NAME,
        "push_target": push_target,
        "origin": origin,
    }


def _repo_id(root: str) -> str:
    from .util import repo_id

    return repo_id(root)


def _dir_size(path: Path) -> int:
    """On-disk size in bytes, counting allocated blocks like `du`.

    Block accounting (not st_size) is what makes gc's win visible: thousands of
    tiny loose refs each pin a full disk block, so packing them reclaims far
    more on disk than their content bytes suggest.
    """
    total = 0
    for p in path.rglob("*"):
        try:
            st = p.stat()
            blocks = getattr(st, "st_blocks", None)
            total += blocks * 512 if blocks is not None else st.st_size
        except OSError:
            continue
    return total


def gc_bare(bare: Path) -> tuple[int, int]:
    """Repack/prune one bare gate repo. Returns (bytes_before, bytes_after).

    The gate is daemonless: receive-pack/fetch can be writing loose objects
    into this bare repo concurrently (a different push, or a `greenlight run`
    fetch) while gc runs, and that object-writing happens before our hook so we
    can't lock it out. We therefore keep git's default prune grace period
    (gc.pruneExpire) instead of `--prune=now`, so gc never deletes
    just-written-but-not-yet-referenced objects and corrupts an in-flight
    operation. The disk win comes from packing the many loose objects/refs,
    which plain gc still does; only recent dangling objects survive to the next
    gc.
    """
    before = _dir_size(bare)
    # Prune dangling worktree admin entries first so their refs don't pin objects.
    run(["git", "worktree", "prune"], cwd=bare)
    run(["git", "gc"], cwd=bare, check=True)
    return before, _dir_size(bare)


def _provision_bare(bare: Path, origin: str) -> None:
    if not (bare / "HEAD").exists():
        run(["git", "init", "--bare", str(bare)], check=True)
    # Allow push options (carry --intent etc. through the push).
    run(["git", "config", "receive.advertisePushOptions", "true"], cwd=bare, check=True)
    # Record upstream as origin on the bare repo so `gh` can resolve repo
    # context from worktrees created off the gate.
    run(["git", "remote", "remove", "origin"], cwd=bare)
    run(["git", "remote", "add", "origin", origin], cwd=bare, check=True)
    # Pin hookspath so a subprocess (husky etc.) can't disable our hook.
    run(["git", "config", "core.hooksPath", str(bare / "hooks")], cwd=bare, check=True)


def _install_hook(bare: Path, work_root: str) -> None:
    hooks = bare / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "post-receive"
    hook.write_text(
        _HOOK_TEMPLATE.format(
            python=sys.executable or "python3",
            bare=str(bare),
            work=work_root,
        )
    )
    hook.chmod(0o755)


def _ensure_remote(root: str, bare: Path) -> None:
    existing = gitx.remote_url(root, REMOTE_NAME)
    if existing == str(bare):
        return
    if existing:
        run(["git", "remote", "set-url", REMOTE_NAME, str(bare)], cwd=root, check=True)
    else:
        run(["git", "remote", "add", REMOTE_NAME, str(bare)], cwd=root, check=True)


# Vars git exports into hook subprocesses that would otherwise pin every git
# call to the bare repo and break cwd-based operations. We must drop these
# before running the pipeline in a worktree. GIT_PUSH_OPTION_* is preserved so
# intent passed via `git push -o intent=...` still reaches us.
_LEAKED_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_PREFIX",
    "GIT_CONFIG_PARAMETERS",
    "GIT_INTERNAL_GETTEXT_TEST_MESSAGES",
)


def scrub_git_env() -> None:
    """Remove leaked GIT_* env so worktree git commands honor their cwd."""
    for key in _LEAKED_GIT_ENV:
        os.environ.pop(key, None)


def read_pushed_refs() -> list[tuple[str, str, str]]:
    """Parse post-receive stdin: 'oldsha newsha refname' per line."""
    refs = []
    for line in sys.stdin:
        parts = line.split()
        if len(parts) == 3:
            refs.append((parts[0], parts[1], parts[2]))
    return refs


def parse_push_options() -> dict[str, str]:
    """Read push options injected by git into GIT_PUSH_OPTION_* env vars."""
    opts: dict[str, str] = {}
    count = int(os.environ.get("GIT_PUSH_OPTION_COUNT", "0") or "0")
    for i in range(count):
        raw = os.environ.get(f"GIT_PUSH_OPTION_{i}", "")
        if "=" in raw:
            k, v = raw.split("=", 1)
            opts[k.strip()] = v.strip()
    return opts
