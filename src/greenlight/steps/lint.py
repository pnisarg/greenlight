"""Format + lint step.

Runs the configured format and lint commands in the worktree. If lint fails
and the diff is dirty afterward (formatter rewrote files) we commit the
mechanical fixes. A hard lint failure that the formatter can't resolve is asked
of the agent as a bounded auto-fix, since these are non-functional by nature.
"""
from __future__ import annotations

from .. import gitx
from ..agent import Agent
from ..config import Config
from ..util import info, ok, run, step, warn
from .types import StepResult


def _commit_if_dirty(work_dir: str, message: str) -> bool:
    status = run(["git", "status", "--porcelain"], cwd=work_dir).out.strip()
    if not status:
        return False
    gitx.git(["add", "-A"], work_dir)
    gitx.git(["commit", "-m", message, "--no-verify"], work_dir)
    return True


def run_step(agent: Agent, work_dir: str, cfg: Config) -> StepResult:
    step("format + lint")
    if not cfg.format_cmd and not cfg.lint_cmd:
        info("no format/lint commands configured; skipping")
        return StepResult(name="lint", passed=True, skipped=True)

    deadline = getattr(agent, "deadline", None)

    def _clamp(t: float) -> float:
        return deadline.clamp(t) if deadline is not None else t

    if cfg.format_cmd:
        info(f"$ {cfg.format_cmd}")
        run(["bash", "-lc", cfg.format_cmd], cwd=work_dir, timeout=_clamp(600))
        if _commit_if_dirty(work_dir, "style: apply formatter"):
            info("committed formatter changes")

    if not cfg.lint_cmd:
        ok("formatted")
        return StepResult(name="lint", passed=True)

    info(f"$ {cfg.lint_cmd}")
    r = run(["bash", "-lc", cfg.lint_cmd], cwd=work_dir, timeout=_clamp(600))
    if r.ok:
        ok("lint clean")
        return StepResult(name="lint", passed=True)

    warn("lint failed; asking agent to fix (non-functional only)")
    prompt = (
        "The lint command failed. Fix ONLY the lint/formatting/style errors it "
        "reports. Do not change program behavior, logic, or public APIs. Do not "
        "refactor. After fixing, the lint command must pass.\n\n"
        f"Lint command: {cfg.lint_cmd}\n\n"
        f"Output:\n{(r.out + r.err)[-4000:]}"
    )
    agent.run(prompt, cwd=work_dir, timeout=900)
    _commit_if_dirty(work_dir, "style: fix lint errors")

    r2 = run(["bash", "-lc", cfg.lint_cmd], cwd=work_dir, timeout=_clamp(600))
    if r2.ok:
        ok("lint clean after fix")
        return StepResult(name="lint", passed=True, summary="lint fixed by agent")
    return StepResult(
        name="lint",
        passed=False,
        summary="lint still failing after auto-fix",
    )
