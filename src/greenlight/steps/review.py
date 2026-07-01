"""The review loop — greenlight's core.

Each configured reviewer runs as an independent read-only pi agent with its own
focus/skill (brutal code review, security, ...). Their findings are pooled. If
any blocking findings remain, a single fix agent addresses them — under a strict
rule that it must preserve the captured intent (never delete intentional code or
change product behavior to silence a finding). Then every reviewer runs again
against the new diff. Repeat until clean or max rounds.

Reviewers never write; only the fix agent does. This keeps review honest and the
intent stable.
"""
from __future__ import annotations


from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .. import events
from ..agent import Agent
from ..config import Config, Reviewer
from ..util import GreenlightError, info, ok, step, warn
from .types import Finding, StepResult

# A reviewer that returns no parseable verdict (timed out, pi failed, or emitted
# prose instead of the findings schema) must never be read as "clean" — that is
# a false green light, the worst failure mode for a gate. We fail the gate closed
# with a synthesized blocking finding so the card and review-log show why; the
# specific cause (so the operator knows whether to bump the timeout, check the
# gateway, or check the model) is appended per failure.
_INCONCLUSIVE_DESC = (
    "reviewer returned no usable verdict; failing the gate closed rather than "
    "treating an un-run review as clean"
)


@dataclass
class _Verdict:
    """Outcome of one reviewer invocation.

    findings is None when the verdict is inconclusive (no parseable findings
    list); reason carries the diagnostic, and timed_out marks a hard timeout
    (pi exit 124) — a hung reviewer that a retry won't fix, vs. a transient blip
    that one might.
    """

    findings: list[Finding] | None
    reason: str = ""
    timed_out: bool = False

_REVIEW_SCHEMA_HINT = (
    'Return ONLY a fenced ```json block with this shape:\n'
    '{"findings": [{"severity": "error|warning|info", "file": "path", '
    '"line": <int or null>, "description": "..."}], '
    '"summary": "one sentence"}'
)


def _reviewer_prompt(r: Reviewer, base: str, head: str, intent: str) -> str:
    return f"""You are reviewing a code change. Your review focus:

{r.focus or "General correctness and quality review."}

The change's INTENT (ground truth — do NOT flag the author's deliberate choices
as mistakes; only flag genuine defects):
\"\"\"
{intent}
\"\"\"

Scope: the diff between {base} and {head}. Read the diff and the surrounding
code yourself (git diff {base}..{head}). Inspect call sites and helpers as
needed.

Rules:
- Anchor each finding to a file and 1-indexed line in the changed code.
- severity "error" = must not merge; "warning" = should fix; "info" = optional.
- Be concrete and actionable. No style/formatting/lint nits (a separate step
  handles those). No generic advice like "add more tests".
- If the change is clean for your focus area, return an empty findings array.
- Do NOT run tests or modify any files. Review only.

{_REVIEW_SCHEMA_HINT}"""


def _parse_findings(payload, reviewer: str) -> list[Finding]:
    if not isinstance(payload, dict):
        return []
    out = []
    for f in payload.get("findings", []) or []:
        if not isinstance(f, dict):
            continue
        out.append(
            Finding(
                severity=str(f.get("severity", "warning")).lower(),
                file=str(f.get("file", "")),
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                description=str(f.get("description", "")).strip(),
                reviewer=reviewer,
            )
        )
    return out


def _review_once(
    agent: Agent, work_dir: str, r: Reviewer, base: str, head: str, intent: str
) -> _Verdict:
    """Run one reviewer. Returns a _Verdict whose findings is None when the
    verdict is inconclusive — the agent raised, or its output had no `findings`
    list (prose, truncated JSON, a degraded gateway). None must never be read as
    "clean"; the reason/timed_out fields let the caller report and route it.
    """
    skills = [r.skill] if r.skill else None
    try:
        res = agent.run(
            _reviewer_prompt(r, base, head, intent),
            cwd=work_dir,
            read_only=True,
            skills=skills,
            timeout=1200,
        )
    except GreenlightError as exc:
        msg = str(exc).splitlines()[0]
        # agent.run formats the message as "pi invocation failed (<code>): ...";
        # exit 124 is the timeout convention (util.run), so a hung reviewer.
        return _Verdict(None, reason=f"pi failed: {msg}", timed_out="(124)" in msg)
    payload = res.json()
    if not (isinstance(payload, dict) and isinstance(payload.get("findings"), list)):
        if res.code == 124:
            return _Verdict(None, reason="pi timed out (exit 124) with unparseable output",
                            timed_out=True)
        reason = (
            f"pi exited {res.code} without the findings schema"
            if res.code
            else "pi returned prose / invalid JSON, not the findings schema"
        )
        return _Verdict(None, reason=reason)
    return _Verdict(_parse_findings(payload, r.name))


def _inconclusive_finding(reviewer: str, reason: str) -> Finding:
    return Finding(
        severity="error",
        file="",
        line=None,
        description=f"{_INCONCLUSIVE_DESC} (cause: {reason})",
        reviewer=reviewer,
    )


def _review_with_retry(
    agent: Agent, work_dir: str, r: Reviewer, base: str, head: str, intent: str
) -> _Verdict:
    """Run one reviewer with a single transient-blip retry.

    Retries once to absorb a transient gateway blip — but not a hard timeout,
    which is a hung reviewer a retry only doubles the latency of (notably in the
    uncapped run_timeout=0 path, where the Deadline doesn't clamp it).
    """
    v = _review_once(agent, work_dir, r, base, head, intent)
    if v.findings is None and not v.timed_out:
        warn(f"  {r.name}: {v.reason}; retrying once")
        v = _review_once(agent, work_dir, r, base, head, intent)
    return v


def _run_reviewers(
    agent: Agent, work_dir: str, cfg: Config, base: str, head: str, intent: str, rnd: int
) -> tuple[list[Finding], list[str]]:
    """Run every enabled reviewer concurrently. Returns (findings, inconclusive).

    Each reviewer is its own read-only pi subprocess, so they fan out across a
    thread pool and the round costs the slowest reviewer's wall time instead of
    the sum. A reviewer that yields no usable verdict is retried once; if it
    still fails it is recorded as inconclusive (with a synthesized blocking
    finding for the card and review-log) so the caller can fail the gate closed
    instead of shipping an un-reviewed change.
    """
    findings: list[Finding] = []
    inconclusive: list[str] = []
    enabled = [r for r in cfg.reviewers if r.enabled]
    run_model = getattr(agent, "model", "")
    # Each reviewer may run on its own model (falling back to review_model, then
    # the run model); build one pi wrapper per distinct model, reusing the run
    # agent for reviewers that inherit its model.
    agents = _review_agents(agent, cfg)
    # Emit all start events up front so the live card shows every reviewer as
    # running at once, then fan out the work.
    for r in enabled:
        model = _effective_model(cfg, r, run_model)
        info(f"reviewer: {r.name}" + (f" [{model}]" if model else ""))
        events.emit(
            "reviewer",
            name=r.name,
            round=rnd,
            findings=None,
            blocking=None,
            model=model or None,
        )

    # Index-keyed (not name-keyed) so duplicate reviewer names can't collide and
    # silently drop one reviewer's verdict.
    verdicts: list[_Verdict | None] = [None] * len(enabled)
    with ThreadPoolExecutor(max_workers=len(enabled) or 1) as ex:
        futures = {
            ex.submit(
                _review_with_retry,
                agents[_effective_model(cfg, r, run_model)],
                work_dir,
                r,
                base,
                head,
                intent,
            ): i
            for i, r in enumerate(enabled)
        }
        for fut in as_completed(futures):
            verdicts[futures[fut]] = fut.result()

    # Aggregate in config order so findings and the review-log stay deterministic
    # regardless of which reviewer finished first.
    for r, v in zip(enabled, verdicts):
        assert v is not None  # every future was awaited above
        if v.findings is None:
            warn(f"  {r.name}: {v.reason}; failing the gate closed")
            synth = _inconclusive_finding(r.name, v.reason)
            inconclusive.append(r.name)
            findings.append(synth)
            events.emit(
                "reviewer",
                name=r.name,
                round=rnd,
                findings=1,
                blocking=1,
                items=[_finding_event(synth, True)],
            )
            continue
        rf = v.findings
        blocking = [f for f in rf if f.blocks(r.blocking_severity)]
        info(f"  {r.name}: {len(rf)} findings ({len(blocking)} blocking)")
        events.emit(
            "reviewer",
            name=r.name,
            round=rnd,
            findings=len(rf),
            blocking=len(blocking),
            items=[_finding_event(f, f.blocks(r.blocking_severity)) for f in rf],
        )
        findings.extend(rf)
    return findings, inconclusive


def _effective_model(cfg: Config, reviewer: Reviewer, run_model: str) -> str:
    """The pi model one reviewer runs on.

    Precedence: the reviewer's own `model`, then the step-level `review_model`,
    then the run's `model`. Empty at every level means "let pi pick its default"
    — so a reviewer inherits the coding agent's model unless it (or the review
    step) explicitly overrides it.
    """
    for candidate in (reviewer.model, cfg.review_model, run_model):
        m = (candidate or "").strip()
        if m:
            return m
    return ""


def _review_agents(agent: Agent, cfg: Config) -> dict[str, Agent]:
    """One Agent per distinct effective reviewer model, keyed by model string.

    Reviewers can each run on their own model (e.g. GPT-5.5 for security while
    the coding/fix agent stays on Claude Opus) without changing the model used
    for intent/fix/CI. The run agent is reused whenever a reviewer's effective
    model matches it, so we only spawn an extra pi wrapper for reviewers that
    actually opt into a different model.
    """
    run_model = getattr(agent, "model", "")
    agents: dict[str, Agent] = {}
    for r in cfg.reviewers:
        if not r.enabled:
            continue
        model = _effective_model(cfg, r, run_model)
        if model in agents:
            continue
        agents[model] = (
            agent
            if model == run_model
            else Agent(
                model=model,
                extra_args=getattr(agent, "extra_args", []),
                deadline=getattr(agent, "deadline", None),
            )
        )
    return agents


def _finding_event(f: Finding, blocks: bool) -> dict:
    return {
        "severity": f.severity,
        "file": f.file,
        "line": f.line,
        "description": f.description,
        "blocks": blocks,
    }


def _blocking(findings: list[Finding], cfg: Config) -> list[Finding]:
    thresh = {r.name: r.blocking_severity for r in cfg.reviewers}
    return [f for f in findings if f.blocks(thresh.get(f.reviewer, "warning"))]


def _fix_prompt(findings: list[Finding], intent: str) -> str:
    items = "\n".join(f"- {f.render()}" for f in findings)
    return f"""Address these review findings by editing the code directly.

The change's INTENT (this is GROUND TRUTH and MUST be preserved):
\"\"\"
{intent}
\"\"\"

Findings to address:
{items}

Hard rules:
- NEVER change the intent. Do not delete or revert the author's intentional
  code, and do not alter product behavior, just to silence a finding. Fix
  forward (add validation, handle edge cases, tighten logic).
- First verify each finding is legitimate. If a finding contradicts the intent
  or is wrong, leave the code and note it; do not "fix" it.
- Make the smallest correct root-cause fix. No unrelated refactoring.
- Do not add comments that merely restate the code.
- Do not run tests (a later step does). Just apply the fixes.
"""


def run_step(
    agent: Agent,
    work_dir: str,
    cfg: Config,
    base: str,
    head: str,
    intent: str,
    commit_fn,
) -> StepResult:
    """Run the review->fix loop. commit_fn(message) commits the worktree."""
    step("review loop")
    all_findings: list[Finding] = []
    # Reviewers may each run on their own model (see _review_agents); the fix
    # agent always keeps the run's model, so fixes never drift off the coding
    # agent's model.

    deadline = getattr(agent, "deadline", None)
    for rnd in range(1, cfg.max_review_rounds + 1):
        if deadline is not None and deadline.expired():
            warn(f"run budget exhausted; stopping review after {rnd - 1} round(s)")
            return StepResult(
                name="review",
                passed=False,
                summary=f"run budget exhausted after {rnd - 1} round(s)",
                findings=all_findings,
            )
        info(f"round {rnd}/{cfg.max_review_rounds}")
        events.emit("review_round", round=rnd, max_rounds=cfg.max_review_rounds)
        findings, inconclusive = _run_reviewers(
            agent, work_dir, cfg, base, head, intent, rnd
        )
        all_findings = findings
        if inconclusive:
            # Infrastructure failure, not a code defect: the fix agent can't
            # repair a flaky reviewer, so don't enter the fix loop. Fail fast and
            # loud — the review didn't actually run.
            names = ", ".join(inconclusive)
            warn(f"review inconclusive: {names} returned no usable verdict")
            return StepResult(
                name="review",
                passed=False,
                summary=f"review inconclusive ({names}); failed closed",
                findings=findings,
            )
        blocking = _blocking(findings, cfg)
        if not blocking:
            ok(f"review clean ({len(findings)} non-blocking notes)")
            return StepResult(
                name="review",
                passed=True,
                summary=f"clean after {rnd} round(s)",
                findings=findings,
            )

        if rnd == cfg.max_review_rounds:
            warn(f"{len(blocking)} blocking findings remain after max rounds")
            return StepResult(
                name="review",
                passed=False,
                summary=f"{len(blocking)} blocking findings unresolved",
                findings=blocking,
            )

        info(f"fixing {len(blocking)} blocking findings (intent-preserving)")
        events.emit("fix", round=rnd, findings=len(blocking))
        agent.run(_fix_prompt(blocking, intent), cwd=work_dir, timeout=1800)
        if commit_fn(f"fix: address review findings (round {rnd})"):
            # New head after the fix commit so the next round reviews the result.
            from .. import gitx

            head = gitx.rev_parse(work_dir, "HEAD") or head

    return StepResult(name="review", passed=False, summary="review loop exhausted",
                      findings=all_findings)
