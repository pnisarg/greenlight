"""Thin git helpers used by the gate and pipeline."""
from __future__ import annotations

from pathlib import Path

from .util import GreenlightError, run

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(args: list[str], cwd: str | Path, check: bool = True, **kw):
    return run(["git", *args], cwd=cwd, check=check, **kw)


def main_repo_root(start: str | Path) -> str:
    """Resolve the main working tree root, normalizing from linked worktrees."""
    r = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=start)
    if not r.ok:
        raise GreenlightError(f"not a git repository: {start}")
    common = Path(r.out.strip())
    # --git-common-dir points at the shared .git; its parent is the main root.
    if common.name == ".git":
        return str(common.parent)
    # bare or unusual layout: fall back to toplevel
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=start)
    return top.out.strip() or str(common.parent)


def remote_url(cwd: str | Path, name: str) -> str | None:
    r = run(["git", "remote", "get-url", name], cwd=cwd)
    return r.out.strip() if r.ok else None


def default_branch(cwd: str | Path, remote: str = "origin") -> str:
    r = run(["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"], cwd=cwd)
    if r.ok and r.out.strip():
        return r.out.strip().rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        if run(["git", "rev-parse", "--verify", f"refs/remotes/{remote}/{cand}"], cwd=cwd).ok:
            return cand
    return "main"


def current_branch(cwd: str | Path) -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).out.strip()


def rev_parse(cwd: str | Path, ref: str) -> str | None:
    r = run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
    return r.out.strip() if r.ok else None


def merge_base(cwd: str | Path, a: str, b: str) -> str | None:
    r = run(["git", "merge-base", a, b], cwd=cwd)
    return r.out.strip() if r.ok else None


def changed_files(cwd: str | Path, base: str, head: str) -> list[str]:
    # Space form (not A..B) so a tree endpoint like the empty-tree SHA works.
    r = run(["git", "diff", "--name-only", base, head], cwd=cwd)
    return [ln.strip() for ln in r.out.splitlines() if ln.strip()]


def diff(cwd: str | Path, base: str, head: str) -> str:
    return run(["git", "diff", base, head], cwd=cwd).out


def is_zero_sha(sha: str) -> bool:
    return set(sha.strip()) <= {"0"} and len(sha.strip()) >= 7
