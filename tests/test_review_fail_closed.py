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


# A `findings` list whose *items* don't match the schema is not a verdict
# either: coercing junk into a Finding (defaulting severity to "warning",
# file/description to "") invents review output nobody produced, and a bad
# severity could downgrade an item below the blocking threshold — a false green
# light. Each case below is one structural way a finding can be malformed.
_OMIT = object()  # marks a key the reviewer left out entirely


def _finding(**over) -> dict:
    base = {"severity": "error", "file": "a.py", "line": 1, "description": "boom"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not _OMIT}


_MALFORMED = [
    ("non_object_string", {"findings": ["not an object"]}, "not an object"),
    ("non_object_null", {"findings": [None]}, "not an object"),
    ("non_object_list", {"findings": [[{"severity": "error"}]]}, "not an object"),
    ("unknown_severity", {"findings": [_finding(severity="critical")]}, "severity"),
    ("non_string_severity", {"findings": [_finding(severity=2)]}, "severity"),
    ("missing_severity", {"findings": [_finding(severity=_OMIT)]}, "severity"),
    ("empty_description", {"findings": [_finding(description="   ")]}, "description"),
    ("missing_description", {"findings": [_finding(description=_OMIT)]}, "description"),
    ("non_string_description", {"findings": [_finding(description=42)]}, "description"),
    # ESC/CR in a description would let reviewer text repaint the operator's
    # terminal card (forging a green "review clean" line).
    ("escape_in_description",
     {"findings": [_finding(description="clean \x1b[32m ok all good")]}, "description"),
    ("carriage_return_in_description",
     {"findings": [_finding(description="bug\r ok  review clean")]}, "description"),
    ("empty_file", {"findings": [_finding(file="")]}, "file"),
    ("blank_file", {"findings": [_finding(file="   ")]}, "file"),
    ("missing_file", {"findings": [_finding(file=_OMIT)]}, "file"),
    ("non_string_file", {"findings": [_finding(file=7)]}, "file"),
    ("absolute_file", {"findings": [_finding(file="/etc/passwd")]}, "file"),
    ("traversal_file", {"findings": [_finding(file="../../etc/passwd")]}, "file"),
    ("backslash_traversal_file",
     {"findings": [_finding(file="..\\..\\secrets.env")]}, "file"),
    ("newline_in_file", {"findings": [_finding(file="a.py\nb.py")]}, "file"),
    ("home_relative_file", {"findings": [_finding(file="~/.ssh/id_rsa")]}, "file"),
    ("zero_line", {"findings": [_finding(line=0)]}, "line"),
    ("negative_line", {"findings": [_finding(line=-3)]}, "line"),
    ("bool_line", {"findings": [_finding(line=True)]}, "line"),
    ("string_line", {"findings": [_finding(line="12")]}, "line"),
    ("float_line", {"findings": [_finding(line=12.5)]}, "line"),
    # One bad item poisons the whole verdict: we can't tell which of the
    # reviewer's other conclusions survived whatever produced the bad one.
    ("valid_item_alongside_malformed",
     {"findings": [_finding(), _finding(severity="critical")]}, "finding 1"),
]


@pytest.mark.parametrize(
    "payload,expected", [(p, e) for _, p, e in _MALFORMED],
    ids=[i for i, _, _ in _MALFORMED],
)
def test_malformed_finding_fails_closed(tmp_path, payload, expected):
    """A structurally invalid finding is retried once (a schema slip can be a
    transient model glitch) and then fails the gate closed, with the reason
    naming the offending field."""
    agent = _ScriptedAgent([payload, payload])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 2
    assert inconclusive == ["brutal"]
    assert len(findings) == 1  # only the synthesized blocking finding
    assert findings[0].severity == "error"
    assert findings[0].blocks("warning")
    assert expected in findings[0].description


def test_malformed_findings_are_not_partially_kept(tmp_path):
    """The valid sibling of a malformed finding must not leak through as if the
    reviewer had reported only it."""
    payload = {"findings": [_finding(description="real bug"), _finding(line=0)]}
    findings, _ = review._run_reviewers(
        _ScriptedAgent([payload, payload]), str(tmp_path), _one_reviewer_cfg(),
        "B", "H", "i", 1
    )
    assert [f.description for f in findings] != ["real bug"]
    assert all("no usable verdict" in f.description for f in findings)


def test_malformed_after_hard_timeout_is_not_retried(tmp_path):
    """A hung reviewer (exit 124) that emitted a malformed findings list is
    still a hung reviewer: fail closed without doubling the latency."""
    payload = {"findings": [_finding(severity="critical")]}
    agent = _ScriptedAgent([(json.dumps(payload), 124)])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 1  # NOT retried
    assert inconclusive == ["brutal"]
    assert findings[0].blocks("warning")


def test_malformed_verdict_fails_run_step_without_fixing(tmp_path):
    """End to end: a malformed verdict blocks the gate and never reaches the fix
    agent — there is no code defect to fix, the review didn't produce one."""
    payload = {"findings": [_finding(file="/etc/passwd")]}
    agent = _ScriptedAgent([payload] * 4)

    def _no_commit(_msg):
        raise AssertionError("fix loop must not run on a malformed verdict")

    res = review.run_step(agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i",
                          _no_commit)
    assert res.passed is False
    assert "inconclusive" in res.summary
    assert agent.calls == 2  # one round: ran + retried, then bailed


def test_valid_findings_stay_backward_compatible(tmp_path):
    """Real reviewer output still parses: every severity, an explicit null line,
    an omitted line, extra additive keys, and padded strings."""
    payload = {
        "findings": [
            {"severity": "error", "file": "a.py", "line": 10, "description": "boom"},
            {"severity": "warning", "file": "b.py", "line": None, "description": " pad "},
            {"severity": "info", "file": "c.py", "description": "no line key"},
            {"severity": "ERROR", "file": " d.py ", "line": 2, "description": "shouty",
             "confidence": 0.9},
        ],
        "summary": "four findings",
    }
    agent = _ScriptedAgent([payload])
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert agent.calls == 1  # a real verdict is never retried
    assert inconclusive == []
    assert [f.severity for f in findings] == ["error", "warning", "info", "error"]
    assert [f.line for f in findings] == [10, None, None, 2]
    assert [f.file for f in findings] == ["a.py", "b.py", "c.py", "d.py"]
    assert [f.description for f in findings] == ["boom", "pad", "no line key", "shouty"]
    assert all(f.reviewer == "brutal" for f in findings)


def test_multiline_description_is_still_valid(tmp_path):
    """Newlines and tabs are ordinary prose, not a malformed finding."""
    payload = {"findings": [_finding(description="line one\n\tline two")]}
    findings, inconclusive = review._run_reviewers(
        _ScriptedAgent([payload]), str(tmp_path), _one_reviewer_cfg(), "B", "H", "i", 1
    )
    assert inconclusive == []
    assert findings[0].description == "line one\n\tline two"


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
