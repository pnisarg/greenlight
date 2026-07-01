"""Reviewers run concurrently, and the review step can use a dedicated model."""
import json
import threading
import time

import pytest

from greenlight import events
from greenlight.agent import AgentResult
from greenlight.config import Reviewer, default_config
from greenlight.steps import review


@pytest.fixture(autouse=True)
def _reset_sink():
    events._sink = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    events._sink = None


class _SlowAgent:
    """Each run sleeps, so a sequential loop would take N*delay but a parallel
    fan-out takes ~delay. Tracks max concurrency observed."""

    def __init__(self, delay: float):
        self.delay = delay
        self.model = ""
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, *a, **k) -> AgentResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(self.delay)
        with self._lock:
            self._active -= 1
        return AgentResult(text='```json\n{"findings": []}\n```', code=0)


def test_reviewers_run_in_parallel(tmp_path):
    cfg = default_config()
    cfg.reviewers = [
        Reviewer(name="a", focus="x"),
        Reviewer(name="b", focus="y"),
        Reviewer(name="c", focus="z"),
    ]
    agent = _SlowAgent(delay=0.3)

    start = time.monotonic()
    findings, inconclusive = review._run_reviewers(
        agent, str(tmp_path), cfg, "B", "H", "i", 1
    )
    elapsed = time.monotonic() - start

    assert inconclusive == []
    assert findings == []
    assert agent.max_active == 3  # all three ran at once
    assert elapsed < 0.3 * 3  # well under the sequential cost


def test_findings_are_aggregated_in_config_order(tmp_path):
    """Even when reviewers finish out of order, findings follow config order."""

    class _PerNameAgent:
        model = ""

        def run(self, prompt, *a, **k) -> AgentResult:
            # The reviewer focus is embedded in the prompt; key off it.
            name = "a" if "focus-a" in prompt else "b"
            payload = {"findings": [
                {"severity": "warning", "file": f"{name}.py", "line": 1,
                 "description": f"from-{name}"}
            ]}
            # Make "a" slow so "b" completes first.
            if name == "a":
                time.sleep(0.2)
            return AgentResult(text="```json\n" + json.dumps(payload) + "\n```", code=0)

    cfg = default_config()
    cfg.reviewers = [Reviewer(name="a", focus="focus-a"), Reviewer(name="b", focus="focus-b")]
    findings, _ = review._run_reviewers(
        _PerNameAgent(), str(tmp_path), cfg, "B", "H", "i", 1
    )
    assert [f.reviewer for f in findings] == ["a", "b"]


def test_duplicate_reviewer_names_do_not_collide(tmp_path):
    """Two reviewers sharing a name must both be aggregated, not overwritten."""

    class _NamedAgent:
        model = ""

        def run(self, prompt, *a, **k) -> AgentResult:
            tag = "one" if "focus-one" in prompt else "two"
            payload = {"findings": [
                {"severity": "warning", "file": f"{tag}.py", "line": 1,
                 "description": tag}
            ]}
            return AgentResult(text="```json\n" + json.dumps(payload) + "\n```", code=0)

    cfg = default_config()
    cfg.reviewers = [
        Reviewer(name="dup", focus="focus-one"),
        Reviewer(name="dup", focus="focus-two"),
    ]
    findings, inconclusive = review._run_reviewers(
        _NamedAgent(), str(tmp_path), cfg, "B", "H", "i", 1
    )
    assert inconclusive == []
    assert {f.description for f in findings} == {"one", "two"}


def test_effective_model_precedence():
    """reviewer.model > review_model > run model > pi default ("")."""
    cfg = default_config()
    cfg.model = "run-model"
    cfg.review_model = "review-model"

    # Per-reviewer model wins over everything.
    r_own = Reviewer(name="a", focus="x", model="reviewer-model")
    assert review._effective_model(cfg, r_own, cfg.model) == "reviewer-model"

    # No per-reviewer model -> fall back to review_model.
    r_bare = Reviewer(name="b", focus="y")
    assert review._effective_model(cfg, r_bare, cfg.model) == "review-model"

    # No review_model -> inherit the run model.
    cfg.review_model = ""
    assert review._effective_model(cfg, r_bare, cfg.model) == "run-model"

    # Nothing set anywhere -> pi default (empty string).
    cfg.model = ""
    assert review._effective_model(cfg, r_bare, "") == ""


def test_review_agents_builds_one_wrapper_per_distinct_model(monkeypatch):
    cfg = default_config()
    cfg.model = "base-model"
    cfg.review_model = ""
    cfg.reviewers = [
        Reviewer(name="a", focus="x", model="openai-codex/gpt-5.5:high"),
        Reviewer(name="b", focus="y"),  # inherits base-model
        Reviewer(name="c", focus="z", model="openai-codex/gpt-5.5:high"),  # dedup
        Reviewer(name="d", focus="w", model="x", enabled=False),  # skipped
    ]

    created: list[str] = []

    class _FakeAgent:
        def __init__(self, model="", extra_args=None, deadline=None):
            created.append(model)
            self.model = model
            self.extra_args = extra_args or []
            self.deadline = deadline

    monkeypatch.setattr(review, "Agent", _FakeAgent)

    base = _FakeAgent(model="base-model")
    created.clear()
    agents = review._review_agents(base, cfg)

    # "b" inherits base-model and reuses the run agent (no new wrapper).
    assert agents["base-model"] is base
    # "a"/"c" share one wrapper on the overridden model; "d" is disabled/skipped.
    assert agents["openai-codex/gpt-5.5:high"].model == "openai-codex/gpt-5.5:high"
    assert created == ["openai-codex/gpt-5.5:high"]


def test_run_reviewers_dispatches_each_reviewer_to_its_model(monkeypatch, tmp_path):
    """Each reviewer's findings must come from an agent carrying its own model."""
    cfg = default_config()
    cfg.model = "base-model"
    cfg.review_model = ""
    cfg.reviewers = [
        Reviewer(name="a", focus="x", model="model-A"),
        Reviewer(name="b", focus="y", model="model-B"),
        Reviewer(name="c", focus="z"),  # inherits base-model
    ]

    class _ModelEchoAgent:
        def __init__(self, model="", extra_args=None, deadline=None):
            self.model = model
            self.extra_args = extra_args or []
            self.deadline = deadline

        def run(self, prompt, *a, **k) -> AgentResult:
            payload = {
                "findings": [
                    {"severity": "warning", "file": "f.py", "line": 1,
                     "description": self.model}
                ]
            }
            return AgentResult(text="```json\n" + json.dumps(payload) + "\n```", code=0)

    monkeypatch.setattr(review, "Agent", _ModelEchoAgent)
    base = _ModelEchoAgent(model="base-model")
    findings, inconclusive = review._run_reviewers(
        base, str(tmp_path), cfg, "B", "H", "i", 1
    )
    assert inconclusive == []
    assert {f.reviewer: f.description for f in findings} == {
        "a": "model-A",
        "b": "model-B",
        "c": "base-model",
    }


def test_review_model_still_applies_when_no_per_reviewer_model(monkeypatch):
    cfg = default_config()
    cfg.model = "base-model"
    cfg.review_model = "review-model"
    cfg.reviewers = [Reviewer(name="a", focus="x")]

    created: list[str] = []

    class _FakeAgent:
        def __init__(self, model="", extra_args=None, deadline=None):
            created.append(model)
            self.model = model
            self.extra_args = extra_args or []
            self.deadline = deadline

    monkeypatch.setattr(review, "Agent", _FakeAgent)

    base = _FakeAgent(model="base-model")
    created.clear()
    agents = review._review_agents(base, cfg)
    assert agents["review-model"].model == "review-model"
    assert created == ["review-model"]
