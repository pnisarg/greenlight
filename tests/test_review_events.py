"""The reviewer event carries per-finding detail (items) for the live card."""
import json

import pytest

from greenlight import events
from greenlight.agent import AgentResult
from greenlight.config import Reviewer, default_config
from greenlight.steps import review


class _StubAgent:
    """Returns a canned reviewer JSON payload; ignores the prompt."""

    def __init__(self, payload: dict):
        self._text = "```json\n" + json.dumps(payload) + "\n```"

    def run(self, *a, **k) -> AgentResult:
        return AgentResult(text=self._text, code=0)


@pytest.fixture(autouse=True)
def _reset_sink():
    events._sink = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    events._sink = None


def test_reviewer_event_includes_finding_items(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))

    cfg = default_config()
    cfg.reviewers = [Reviewer(name="brutal", focus="x", blocking_severity="warning")]
    agent = _StubAgent(
        {
            "findings": [
                {"severity": "error", "file": "a.py", "line": 10, "description": "null deref"},
                {"severity": "info", "file": "b.py", "line": None, "description": "nit"},
            ],
            "summary": "two findings",
        }
    )

    review._run_reviewers(agent, str(tmp_path), cfg, "BASE", "HEAD", "intent", 1)

    recs = [json.loads(line) for line in path.read_text().splitlines()]
    completed = [r for r in recs if r["type"] == "reviewer" and r["findings"] is not None]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["findings"] == 2
    assert ev["blocking"] == 1  # error blocks at warning threshold; info does not
    items = ev["items"]
    assert len(items) == 2
    assert items[0] == {
        "severity": "error",
        "file": "a.py",
        "line": 10,
        "description": "null deref",
        "blocks": True,
    }
    assert items[1]["blocks"] is False
    assert items[1]["line"] is None


def test_reviewer_started_event_carries_effective_model(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))

    class _ModelAgent:
        def __init__(self, model="", extra_args=None, deadline=None):
            self.model = model

        def run(self, *a, **k) -> AgentResult:
            return AgentResult(text='```json\n{"findings": []}\n```', code=0)

    monkeypatch.setattr(review, "Agent", _ModelAgent)

    cfg = default_config()
    cfg.model = ""  # run/global default = pi default
    cfg.reviewers = [
        Reviewer(name="sec", focus="x", model="openai-codex/gpt-5.5:high"),
        Reviewer(name="brutal", focus="y"),  # inherits pi default
    ]
    review._run_reviewers(_ModelAgent(model=""), str(tmp_path), cfg, "B", "H", "i", 1)

    recs = [json.loads(line) for line in path.read_text().splitlines()]
    started = {
        r["name"]: r
        for r in recs
        if r["type"] == "reviewer" and r["findings"] is None
    }
    assert started["sec"]["model"] == "openai-codex/gpt-5.5:high"
    assert started["brutal"]["model"] is None  # pi default -> null, not a name


def test_started_event_has_no_items(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))
    cfg = default_config()
    cfg.reviewers = [Reviewer(name="brutal", focus="x")]
    review._run_reviewers(_StubAgent({"findings": []}), str(tmp_path), cfg, "B", "H", "i", 1)

    recs = [json.loads(line) for line in path.read_text().splitlines()]
    started = [r for r in recs if r["type"] == "reviewer" and r["findings"] is None]
    assert len(started) == 1
    assert "items" not in started[0]
