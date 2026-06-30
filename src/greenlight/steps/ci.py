"""Post-PR CI monitoring + intent-preserving auto-fix.

Once the branch is forwarded and a PR is opened, the *real* remote CI is the
authoritative test signal: it has installed deps, services, and caching that
greenlight's throwaway worktree can't reproduce. This step polls the PR's check
rollup until it settles; on failure it pulls the failed job logs, runs an
intent-preserving fix agent, re-pushes, and re-polls — up to a bounded number of
rounds. The gate only reports green once CI is green.

GitHub only for now (via the `gh` CLI). It no-ops (skips, passing) when ci is
disabled, gh is unavailable, no PR was opened, or the PR carries no checks.

Pure helpers (`_normalize_rollup`, `_classify`, `_interval`) hold the logic and
are unit-tested; `run_step` is the I/O shell around them.
"""
from __future__ import annotations

import json
import time

from .. import events, gitx
from ..agent import Agent
from ..config import Config
from ..util import Deadline, GreenlightError, info, ok, run, step, warn, which
from .types import Finding, StepResult

# GitHub check conclusions that mean a check went red. NEUTRAL/SKIPPED/SUCCESS
# are treated as passing; anything here blocks.
_FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
    "STALE", "ERROR",
}
_OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

# Trust an empty rollup only after this grace window — checks may not have
# registered the instant the PR opens.
_EMPTY_GRACE = 90
# After re-pushing a fix, give the new CI run a moment to supersede the old
# (otherwise the rollup still shows the prior, settled state).
_POST_PUSH_DELAY = 30
# Number of consecutive all-green polls before trusting a pass. Guards against a
# false green when a slower workflow's checks haven't registered on the PR yet
# (with empty required_checks the rollup only reflects checks that exist so far).
_GREEN_STABLE = 2
# Per-job and total caps on failed-log text fed to the fix agent.
_LOG_PER_JOB = 6000
_LOG_TOTAL = 24000


def _req_match(required: str, name: str) -> bool:
    return required == name or required.lower() in name.lower()


def _normalize_rollup(rollup: list) -> list[dict]:
    """Flatten GitHub's statusCheckRollup into uniform check dicts.

    The rollup mixes Checks API `CheckRun`s and legacy commit-status
    `StatusContext`s; normalize both to {name, done, ok, failed, url}.
    """
    out: list[dict] = []
    for c in rollup or []:
        if not isinstance(c, dict):
            continue
        if c.get("__typename") == "StatusContext" or "context" in c:
            name = str(c.get("context") or "")
            state = str(c.get("state") or "").upper()
            done = state not in ("", "PENDING", "EXPECTED")
            failed = state in ("FAILURE", "ERROR")
            okk = state == "SUCCESS"
            url = str(c.get("targetUrl") or "")
        else:
            name = str(c.get("name") or c.get("workflowName") or "")
            status = str(c.get("status") or "").upper()
            conclusion = str(c.get("conclusion") or "").upper()
            done = status == "COMPLETED" or (status == "" and conclusion != "")
            failed = conclusion in _FAIL_CONCLUSIONS
            okk = conclusion in _OK_CONCLUSIONS
            url = str(c.get("detailsUrl") or "")
        out.append({"name": name, "done": done, "ok": okk, "failed": failed, "url": url})
    return out


def _select(checks: list[dict], required: list[str]) -> list[dict]:
    if not required:
        return checks
    return [c for c in checks if any(_req_match(r, c["name"]) for r in required)]


def _classify(checks: list[dict], required: list[str]) -> tuple[str, str, list[str]]:
    """Return (verdict, summary, failed_names).

    verdict ∈ {"empty","pending","pass","fail"}. "empty" means no relevant
    checks reported yet (caller decides skip-vs-wait via the grace window).
    """
    if required:
        missing = [r for r in required if not any(_req_match(r, c["name"]) for c in checks)]
        if missing:
            return ("pending", f"waiting for required check(s): {', '.join(missing)}", [])
    sel = _select(checks, required)
    if not sel:
        return ("empty", "no CI checks reported", [])
    pending = [c for c in sel if not c["done"]]
    if pending:
        return ("pending", f"{len(pending)}/{len(sel)} check(s) still running", [])
    failed = [c["name"] for c in sel if c["failed"]]
    if failed:
        return ("fail", f"{len(failed)} check(s) failed: {', '.join(failed)}", failed)
    return ("pass", f"{len(sel)} check(s) passed", [])


def _interval(elapsed: float) -> int:
    """Backoff schedule: poll tight early, then loosen as the run drags on."""
    if elapsed < 300:
        return 30
    if elapsed < 900:
        return 60
    return 120


def _counts(checks: list[dict], required: list[str]) -> tuple[int, int]:
    sel = _select(checks, required)
    return sum(1 for c in sel if c["ok"]), len(sel)


def _fix_prompt(intent: str, failed: list[str], logs: str) -> str:
    names = ", ".join(failed) or "(unnamed checks)"
    return f"""CI failed on the pull request for this change. Fix the failures by
editing code directly in this worktree.

The change's INTENT (GROUND TRUTH — it MUST be preserved):
\"\"\"
{intent}
\"\"\"

Failing checks: {names}

Failed job logs (truncated):
\"\"\"
{logs or "(no logs could be fetched; infer the failure from the check names and the diff)"}
\"\"\"

Hard rules:
- Make the smallest correct root-cause fix. No unrelated refactoring.
- NEVER change the intent. Do not delete or revert the author's intentional
  code, and do not weaken, skip, or delete tests/assertions just to make CI
  pass. Fix the code, not the check.
- If a failure is environmental/flaky and not caused by this change, leave the
  code as-is rather than masking it.
- Do not add comments that merely restate the code.
"""


def _fetch_rollup(work_dir: str, branch: str) -> list[dict]:
    r = run(["gh", "pr", "view", branch, "--json", "statusCheckRollup"], cwd=work_dir)
    if not r.ok:
        warn(f"gh pr view failed: {r.err.strip()[:200]}")
        return []
    try:
        data = json.loads(r.out or "{}")
    except json.JSONDecodeError:
        return []
    return _normalize_rollup(data.get("statusCheckRollup") or [])


def _failed_logs(work_dir: str, branch: str) -> str:
    """Concatenate `gh run --log-failed` for failed runs on the current head."""
    head = gitx.rev_parse(work_dir, "HEAD") or ""
    r = run(["gh", "run", "list", "--branch", branch, "--limit", "30",
             "--json", "databaseId,conclusion,headSha,workflowName,name"], cwd=work_dir)
    try:
        runs = json.loads(r.out or "[]")
    except json.JSONDecodeError:
        runs = []
    chunks: list[str] = []
    total = 0
    for entry in runs if isinstance(runs, list) else []:
        if str(entry.get("conclusion", "")).upper() not in _FAIL_CONCLUSIONS:
            continue
        if head and entry.get("headSha") and entry.get("headSha") != head:
            continue  # stale run from a prior push
        rid = entry.get("databaseId")
        if rid is None:
            continue
        wf = entry.get("workflowName") or entry.get("name") or "workflow"
        lr = run(["gh", "run", "view", str(rid), "--log-failed"], cwd=work_dir, timeout=120)
        log = (lr.out or "").strip()[:_LOG_PER_JOB]
        if not log:
            continue
        chunk = f"=== {wf} (run {rid}) ===\n{log}"
        chunks.append(chunk)
        total += len(chunk)
        if total >= _LOG_TOTAL:
            break
    return "\n\n".join(chunks)


def _fail_result(summary: str, failed: list[str]) -> StepResult:
    findings = [
        Finding(severity="error", file="", line=None,
                description=f"CI check failed: {n}", reviewer="ci")
        for n in failed
    ]
    return StepResult(name="ci", passed=False, summary=summary, findings=findings)


def run_step(
    work_dir: str,
    cfg: Config,
    branch: str,
    intent: str,
    commit_fn,
    forward,
    pr_skipped: bool,
) -> StepResult:
    """Monitor the PR's CI and auto-fix failures. Returns the gate result.

    commit_fn(message) commits the worktree; forward() re-pushes the branch to
    the real remote so a fix triggers a fresh CI run.
    """
    step("ci monitor")
    if cfg.ci_provider != "github":
        warn(f"ci provider '{cfg.ci_provider}' unsupported; skipping")
        events.emit("ci", status="skip")
        return StepResult(name="ci", passed=True, skipped=True, summary="unsupported provider")
    if not which("gh"):
        warn("gh not found; cannot monitor CI")
        events.emit("ci", status="skip")
        return StepResult(name="ci", passed=True, skipped=True, summary="gh unavailable")
    if pr_skipped or forward is None:
        warn("no PR/remote to monitor; skipping CI")
        events.emit("ci", status="skip")
        return StepResult(name="ci", passed=True, skipped=True, summary="no PR to monitor")

    # CI watch gets its own time budget, independent of the run_timeout that
    # bounds the local pipeline — CI legitimately takes many minutes.
    agent = Agent(model=cfg.model, deadline=Deadline(cfg.ci_timeout) if cfg.ci_timeout else None)
    start = time.monotonic()
    poll_start = start  # idle deadline anchor; re-armed on each fix push
    fixes_used = 0
    consecutive_green = 0

    while True:
        if cfg.ci_timeout and time.monotonic() - poll_start > cfg.ci_timeout:
            warn(f"CI did not settle within {cfg.ci_timeout}s; leaving PR open")
            events.emit("ci", status="fail", reason="timeout")
            return StepResult(name="ci", passed=False,
                              summary=f"CI did not settle within {cfg.ci_timeout}s")

        checks = _fetch_rollup(work_dir, branch)
        verdict, summary, failed = _classify(checks, cfg.ci_required_checks)
        passed_n, total_n = _counts(checks, cfg.ci_required_checks)
        info(f"CI: {summary}")
        events.emit("ci", status="running", checks=passed_n, total=total_n, round=fixes_used)

        if verdict != "pass":
            consecutive_green = 0

        if verdict == "empty":
            if time.monotonic() - start > _EMPTY_GRACE:
                ok("no CI checks on this PR; nothing to gate")
                events.emit("ci", status="skip")
                return StepResult(name="ci", passed=True, skipped=True, summary="no CI checks")
            time.sleep(_interval(time.monotonic() - start))
            continue
        if verdict == "pending":
            time.sleep(_interval(time.monotonic() - start))
            continue
        if verdict == "pass":
            consecutive_green += 1
            if consecutive_green >= _GREEN_STABLE:
                ok(summary)
                events.emit("ci", status="pass", checks=passed_n, total=total_n)
                return StepResult(name="ci", passed=True, summary=summary)
            time.sleep(_interval(time.monotonic() - start))
            continue

        # verdict == "fail"
        if fixes_used >= cfg.ci_max_fix_rounds:
            warn(f"CI red after {fixes_used} fix attempt(s); leaving PR open for manual fix")
            events.emit("ci", status="fail", checks=passed_n, total=total_n)
            return _fail_result(summary, failed)

        info(f"CI failed; intent-preserving fix attempt {fixes_used + 1}/{cfg.ci_max_fix_rounds}")
        events.emit("ci_fix", round=fixes_used + 1, findings=len(failed))
        logs = _failed_logs(work_dir, branch)
        try:
            agent.run(_fix_prompt(intent, failed, logs), cwd=work_dir, timeout=1800)
        except GreenlightError as exc:
            warn(f"CI fix agent failed: {str(exc).splitlines()[0]}")
        fixes_used += 1
        if not commit_fn(f"fix: address CI failures (attempt {fixes_used})"):
            warn("CI fix produced no changes; CI won't change — stopping")
            events.emit("ci", status="fail", checks=passed_n, total=total_n)
            return _fail_result(f"{summary}; auto-fix produced no changes", failed)
        if not forward():
            warn("re-push of CI fix failed")
            events.emit("ci", status="fail", checks=passed_n, total=total_n)
            return StepResult(name="ci", passed=False, summary="re-push of CI fix failed")
        poll_start = time.monotonic()  # re-arm the idle deadline for the new run
        time.sleep(_POST_PUSH_DELAY)
