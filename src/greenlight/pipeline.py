"""Pipeline orchestration: intent -> lint -> review loop -> verify -> PR.

Runs synchronously inside a throwaway worktree. Returns True only if every gate
passes; the gate forwards the branch to the push target on True.
"""
from __future__ import annotations

import os

from . import events, gitx
from .agent import Agent
from .config import Config
from .util import Deadline
from .diff import classify
from .steps import ci as ci_step
from .steps import intent as intent_step
from .steps import lint as lint_step
from .steps import pr as pr_step
from .steps import review as review_step
from .steps import verify as verify_step
from .steps.types import StepResult
from .util import fail, info, ok, run


def _committer(work_dir: str):
    def commit(message: str) -> bool:
        status = run(["git", "status", "--porcelain"], cwd=work_dir).out.strip()
        if not status:
            return False
        gitx.git(["add", "-A"], work_dir)
        gitx.git(["commit", "-m", message, "--no-verify"], work_dir)
        return True

    return commit


def _resolve_base(work_dir: str, base_sha: str, default_branch: str) -> str:
    # The caller resolves the base SHA against the real repo and passes it in
    # (the worktree lacks origin/<default>). Trust it when reachable; the empty
    # tree SHA is always reachable and yields a full-history diff.
    if base_sha == gitx.EMPTY_TREE:
        return base_sha
    if base_sha and not gitx.is_zero_sha(base_sha) and gitx.rev_parse(work_dir, base_sha):
        return base_sha
    for ref in (f"origin/{default_branch}", default_branch):
        mb = gitx.merge_base(work_dir, "HEAD", ref)
        if mb:
            return mb
    return gitx.EMPTY_TREE


def run_pipeline(
    work_dir: str,
    cfg: Config,
    branch: str,
    base_sha: str,
    default_branch: str,
    supplied_intent: str | None,
    forward=None,
) -> bool:
    """Run the gate. `forward`, if given, is a no-arg callable returning bool
    that pushes the validated branch to the real remote. It is invoked AFTER
    verification passes and BEFORE the PR step, because opening a PR references
    a branch that must already exist on the remote.
    """
    deadline = Deadline(cfg.run_timeout)
    agent = Agent(model=cfg.model, deadline=deadline)
    commit = _committer(work_dir)
    head = gitx.rev_parse(work_dir, "HEAD")
    base = _resolve_base(work_dir, base_sha, default_branch)
    files = gitx.changed_files(work_dir, base, head)
    if not files:
        ok("no changes to validate; forwarding as-is")
        if forward is not None and not forward():
            return False
        return True
    cls = classify(files, cfg.routing)
    info(f"{len(files)} changed files — classified {cls.label}")
    # Stamp the pipeline PID so a watcher can tell a still-running gate apart from
    # one that was killed mid-run (e.g. the pi window closed): a missing run_end
    # plus a dead PID means abandoned, not slow.
    events.emit("run_start", branch=branch, classification=cls.label, files=files, pid=os.getpid())

    def _budget_check(stage: str) -> bool:
        if deadline.expired():
            fail(f"run budget ({cfg.run_timeout}s) exhausted before {stage}; stopping")
            events.emit("run_end", passed=False)
            return False
        return True

    intent = intent_step.capture(agent, work_dir, base, head, supplied_intent)
    info(f"intent: {intent[:200]}")
    events.emit(
        "intent",
        source="supplied" if supplied_intent and supplied_intent.strip() else "reconstructed",
        text=intent,
    )

    results: list[StepResult] = []

    if not _budget_check("lint"):
        return False
    lint_res = lint_step.run_step(agent, work_dir, cfg)
    results.append(lint_res)
    events.emit(
        "lint",
        status="skip" if lint_res.skipped else ("pass" if lint_res.passed else "fail"),
        fixed="fixed by agent" in lint_res.summary,
    )
    if not lint_res.passed:
        fail("lint gate failed")
        events.emit("run_end", passed=False)
        return False
    head = gitx.rev_parse(work_dir, "HEAD")  # lint may have committed fixes

    if not _budget_check("review"):
        return False
    review_res = review_step.run_step(agent, work_dir, cfg, base, head, intent, commit)
    results.append(review_res)
    if not review_res.passed:
        fail("review gate failed")
        _print_findings(review_res)
        events.emit("run_end", passed=False)
        return False
    head = gitx.rev_parse(work_dir, "HEAD")  # review fixes may have committed

    if not _budget_check("verify"):
        return False
    verify_results = verify_step.run_step(agent, work_dir, cfg, cls, commit)
    results.extend(verify_results)
    for r in verify_results:
        target = "frontend" if "frontend" in r.name else "backend"
        events.emit(
            "verify",
            target=target,
            status="skip" if r.skipped else ("pass" if r.passed else "fail"),
            evidence=r.evidence,
        )
    if any(not r.passed and not r.skipped for r in verify_results):
        fail("verify gate failed")
        events.emit("run_end", passed=False)
        return False

    # Forward to the real remote before opening the PR: `gh pr create --head`
    # needs the branch (and its commits) to already exist on origin.
    if forward is not None and not forward():
        fail("forward failed; nothing shipped")
        events.emit("run_end", passed=False)
        return False

    pr_res = pr_step.run_step(work_dir, cfg, branch, intent, results, default_branch)
    results.append(pr_res)
    events.emit("pr", status=_pr_status(pr_res), url=pr_res.summary)

    # CI monitoring (opt-in): the real remote CI is the authoritative test
    # signal. Poll the PR's checks, auto-fix failures, and only declare green
    # once CI is green. Runs after the PR is open (so checks have fired) and
    # re-uses `forward` to re-push intent-preserving fixes.
    if cfg.ci_enabled:
        ci_res = ci_step.run_step(
            work_dir, cfg, branch, intent, commit, forward,
            pr_skipped=pr_res.skipped,
        )
        results.append(ci_res)
        if not ci_res.passed and not ci_res.skipped:
            fail("ci gate failed")
            events.emit("run_end", passed=False)
            return False

    ok("all gates green")
    events.emit("run_end", passed=True)
    return True


def _pr_status(res: StepResult) -> str:
    if res.skipped:
        return "skip"
    return "open" if res.passed else "fail"


def _print_findings(res: StepResult) -> None:
    for f in res.findings:
        info(f.render())
