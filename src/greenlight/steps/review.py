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


from .. import events
from ..agent import Agent
from ..config import Config, Reviewer
from ..util import info, ok, step, warn
from .types import Finding, StepResult

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


def _run_reviewers(
    agent: Agent, work_dir: str, cfg: Config, base: str, head: str, intent: str, rnd: int
) -> list[Finding]:
    findings: list[Finding] = []
    for r in cfg.reviewers:
        if not r.enabled:
            continue
        info(f"reviewer: {r.name}")
        events.emit("reviewer", name=r.name, round=rnd, findings=None, blocking=None)
        skills = [r.skill] if r.skill else None
        res = agent.run(
            _reviewer_prompt(r, base, head, intent),
            cwd=work_dir,
            read_only=True,
            skills=skills,
            timeout=1200,
        )
        rf = _parse_findings(res.json(), r.name)
        blocking = [f for f in rf if f.blocks(r.blocking_severity)]
        info(f"  {len(rf)} findings ({len(blocking)} blocking)")
        events.emit(
            "reviewer",
            name=r.name,
            round=rnd,
            findings=len(rf),
            blocking=len(blocking),
            items=[_finding_event(f, f.blocks(r.blocking_severity)) for f in rf],
        )
        findings.extend(rf)
    return findings


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
        findings = _run_reviewers(agent, work_dir, cfg, base, head, intent, rnd)
        all_findings = findings
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
