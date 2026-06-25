"""Open a clean PR with intent + verifiable evidence.

The PR body is assembled deterministically from pipeline state (no LLM): the
captured intent, the review summary, and the verification evidence (test
results and/or committed screenshot). Created via `gh` when available.
"""
from __future__ import annotations

from .. import gitx
from ..config import Config
from ..util import ok, run, step, warn, which
from .types import StepResult


def _body(intent: str, results: list[StepResult], branch: str) -> str:
    lines = ["## Intent", "", intent.strip(), "", "## Verification", ""]
    any_ev = False
    for r in results:
        if r.skipped:
            lines.append(f"- _{r.name}: skipped — {r.summary or 'n/a'}_")
            continue
        mark = "✅" if r.passed else "❌"
        lines.append(f"- {mark} **{r.name}** — {r.summary or ('passed' if r.passed else 'failed')}")
        for ev in r.evidence:
            any_ev = True
            if ev.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                lines.append(f"  - ![{r.name}]({ev})")
            else:
                lines.append(f"  - evidence: `{ev}`")
    if not any_ev:
        lines.append("")
        lines.append("_Evidence artifacts committed under the evidence dir._")
    lines += ["", "---", "", "_Opened by greenlight after passing the review loop and verification._"]
    return "\n".join(lines)


def run_step(
    work_dir: str,
    cfg: Config,
    branch: str,
    intent: str,
    results: list[StepResult],
    base_branch: str,
) -> StepResult:
    step("pull request")
    if not which("gh"):
        warn("gh not found; branch is pushed but PR not opened")
        return StepResult(name="pr", passed=True, skipped=True,
                          summary="gh unavailable; open PR manually")

    title = gitx.git(["log", "-1", "--format=%s", branch], work_dir, check=False).out.strip()
    body = _body(intent, results, branch)

    # Idempotent: if a PR already exists for the branch, just report it.
    existing = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=work_dir)
    if existing.ok and existing.out.strip():
        url = existing.out.strip()
        ok(f"PR already open: {url}")
        return StepResult(name="pr", passed=True, summary=url)

    r = run(
        ["gh", "pr", "create", "--base", base_branch, "--head", branch,
         "--title", title or branch, "--body", body],
        cwd=work_dir,
    )
    if not r.ok:
        warn(f"gh pr create failed: {r.err.strip()[:300]}")
        return StepResult(name="pr", passed=False, summary="PR creation failed")
    url = r.out.strip().splitlines()[-1] if r.out.strip() else ""
    ok(f"PR opened: {url}")
    return StepResult(name="pr", passed=True, summary=url)
