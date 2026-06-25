"""Intent capture.

The authoritative source of intent is the coding agent that made the change:
it alone knows the *why* -- what the user asked for, the decisions and tradeoffs
it made, what it deliberately ruled in or out. A diff only shows *what* changed.
So greenlight takes agent-authored intent verbatim and treats it as ground
truth that the review loop must never alter.

greenlight does NOT scan agent transcripts (slow, flaky). When no intent is
supplied (e.g. a bare `git push greenlight` by a human), it falls back to a
cheap one-shot summary of the diff + commits so reviewers have *some* context --
but this is a degraded path: it cannot recover deliberate choices, so the review
loop is noisier. The fix is to pass real intent, not to improve the fallback.
"""
from __future__ import annotations

from .. import gitx
from ..agent import Agent
from ..util import info, step, warn

# Marks a fallback (diff-derived) intent so downstream surfaces can flag that it
# was reconstructed, not authored by the agent that made the change.
FALLBACK_PREFIX = "[reconstructed from diff - no intent was supplied] "


def capture(
    agent: Agent,
    work_dir: str,
    base: str,
    head: str,
    supplied: str | None,
) -> str:
    step("intent")
    if supplied and supplied.strip():
        info("using author-supplied intent")
        return supplied.strip()

    warn("no intent supplied - reconstructing from the diff (degraded; review will be noisier)")
    warn("pass real intent with: greenlight run --intent-file - (or --intent)")
    log = gitx.git(["log", "--format=%s%n%b", f"{base}..{head}"], work_dir, check=False).out
    names = "\n".join(gitx.changed_files(work_dir, base, head))
    prompt = (
        "Summarize the INTENT of this change in 2-4 sentences: what the author "
        "set out to accomplish and any notable decisions or tradeoffs. Write it "
        "as the goal behind the change, not a description of the diff. Output "
        "plain prose only, no preamble.\n\n"
        f"Commit messages:\n{log}\n\nChanged files:\n{names}\n"
    )
    res = agent.run(prompt, cwd=work_dir, read_only=True, timeout=300)
    out = res.text.strip()
    if not out:
        return FALLBACK_PREFIX + "(intent unavailable)"
    return FALLBACK_PREFIX + out
