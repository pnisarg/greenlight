"""The review gate must fail *closed* on an inconclusive reviewer.

A reviewer that returns no usable verdict — the pi call raised (timeout/crash),
or the output had no `findings` list (prose, truncated JSON, a degraded gateway)
— used to be read as "0 findings → clean", a false green light. It now retries
once and, if still inconclusive, fails the review gate with a synthesized
blocking finding rather than shipping an un-reviewed change.
"""
import json

import pytest

from greenlight import events
from greenlight.agent import AgentResult
from greenlight.config import Reviewer, default_config
from greenlight.steps import review
from greenlight.util import GreenlightError


class _ScriptedAgent:
    """Returns/raises per call from a script, so we can model retries."""

    def __init__(self, outcomes):
        # outcomes: list of either a dict payload (JSON), a raw str, an
        # (text, code) tuple to model a non-zero exit, or an Exception to raise.
        self._outcomes = list(outcomes)
        self.deadline = None
        self.calls = 0

    def run(self, *a, **k) -> AgentResult:
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            text, code = outcome
            return AgentResult(text=str(text), code=code)
        if isinstance(outcome, dict):
            return AgentResult(text="```json\n" + json.dumps(outcome) + "\n```", code=0)
        return AgentResult(text=str(outcome), code=0)


@pytest.fixture(autouse=True)
def _reset_sink():
    events._sink = None
    events._mirror = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    events._sink = None
    events._mirror = None


def _one_reviewer_cfg():
    cfg = default_config()
    cfg.reviewers = [Reviewer(name="brutal", focus="x", blocking_severity="warning")]
    return cfg


def test_prose_with_no_findings_list_is_inconclusive_not_clean(tmp_path):
    """An empty `{}` (or prose) lacks a `findings` list — not the same as
    `{"findings": []}`. Both retries return prose → inconclusive."""
    agent = _ScriptedAgent(["I reviewed the code and it looks fine.",
                            "Still looks fine to me."])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 2  # ran once, retried once
    assert inconclusive == ["brutal"]
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].blocks("warning")


def test_agent_error_is_inconclusive(tmp_path):
    """A pi crash (non-timeout GreenlightError) is caught, retried once, and
    treated as inconclusive — never crashing the gate."""
    agent = _ScriptedAgent([GreenlightError("pi invocation failed (1): boom"),
                            GreenlightError("pi invocation failed (1): boom")])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 2
    assert inconclusive == ["brutal"]
    assert findings and findings[0].blocks("warning")
    assert "cause" in findings[0].description


def test_hard_timeout_is_not_retried(tmp_path):
    """A hard timeout (exit 124) is a hung reviewer, not a transient blip:
    fail closed immediately without doubling latency on a retry."""
    # raised-timeout path: agent.run raises with a (124) message.
    agent = _ScriptedAgent([GreenlightError("pi invocation failed (124): timed out")])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 1  # NOT retried
    assert inconclusive == ["brutal"]
    assert "124" in findings[0].description


def test_timeout_with_partial_text_is_not_retried(tmp_path):
    """A timeout that returned partial unparseable text (AgentResult code=124)
    is also a hung reviewer: inconclusive, no retry."""
    agent = _ScriptedAgent([("partial output, no json", 124)])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 1
    assert inconclusive == ["brutal"]
    assert "124" in findings[0].description


def test_retry_recovers_a_transient_blip(tmp_path):
    """First call is inconclusive, retry returns a clean verdict → no failure."""
    agent = _ScriptedAgent([GreenlightError("transient"),
                            {"findings": [], "summary": "clean"}])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 2
    assert inconclusive == []
    assert findings == []


def test_genuinely_empty_findings_is_clean_not_inconclusive(tmp_path):
    """`{"findings": []}` is a real verdict: clean, no retry, not inconclusive."""
    agent = _ScriptedAgent([{"findings": [], "summary": "clean"}])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 1  # no retry on a real verdict
    assert inconclusive == []
    assert findings == []


def test_run_step_fails_closed_and_skips_fix_loop(tmp_path):
    """An inconclusive reviewer fails the gate without entering the fix loop
    (you can't fix a flaky reviewer) and surfaces the synthesized finding."""
    cfg = _one_reviewer_cfg()
    # Every call inconclusive across both the initial run and the retry.
    agent = _ScriptedAgent([GreenlightError("x")] * 4)

    def _no_commit(_msg):  # the fix loop would call this; it must not run
        raise AssertionError("fix loop must not run on an inconclusive review")

    res = review.run_step(agent, str(tmp_path), cfg, "B", "H", "intent", _no_commit)
    assert res.passed is False
    assert "inconclusive" in res.summary
    assert agent.calls == 2  # one round only: ran + retried, then bailed
    assert any(f.severity == "error" for f in res.findings)


def test_inconclusive_emits_blocking_reviewer_event(monkeypatch, tmp_path):
    """The synthesized failure is emitted as a completed reviewer event with a
    blocking item, so the live card and review-log show why the gate failed."""
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))
    agent = _ScriptedAgent(["prose", "prose again"])
    review._run_reviewers(agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1)

    recs = [json.loads(line) for line in path.read_text().splitlines()]
    completed = [r for r in recs if r["type"] == "reviewer" and r["findings"] is not None]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["blocking"] == 1
    assert ev["items"][0]["blocks"] is True
    assert "no usable verdict" in ev["items"][0]["description"]
    assert "cause" in ev["items"][0]["description"]
