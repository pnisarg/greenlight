"""The run-wide Deadline: clamp per-call timeouts and abort gracefully.

Root cause of "stuck for hours": steps each timed out individually, stacking to
multi-hour runs on a degraded gateway. A single shared Deadline clamps every
call to the time left and lets the pipeline stop at the next gate.
"""
import time

from greenlight.agent import Agent
from greenlight.config import default_config
from greenlight.util import Deadline


def test_uncapped_deadline_is_noop():
    d = Deadline(0)
    assert d.remaining() is None
    assert d.expired() is False
    assert d.clamp(1200) == 1200
    assert d.clamp(None) is None


def test_clamp_shrinks_to_remaining_budget():
    d = Deadline(10)
    # A 1200s per-call timeout must shrink to ~the 10s budget, never exceed it.
    clamped = d.clamp(1200)
    assert 0 < clamped <= 10
    # A timeout smaller than the budget is left alone.
    assert d.clamp(2) == 2


def test_expired_budget_clamps_to_small_positive_floor():
    d = Deadline(0.01)
    time.sleep(0.05)
    assert d.expired() is True
    # Never returns 0/None (which would block forever); fails fast instead.
    assert d.clamp(1200) == 0.1
    assert d.clamp(None) == 0.1


def test_default_config_has_run_timeout():
    assert default_config().run_timeout == 1200


def test_agent_clamps_call_timeout(monkeypatch):
    """Agent.run must clamp its per-call timeout to the shared deadline."""
    captured = {}

    def fake_run(args, cwd=None, timeout=None, **k):
        captured["timeout"] = timeout
        from greenlight.util import Run
        return Run(0, '{"ok": true}', "")

    monkeypatch.setattr("greenlight.agent.run", fake_run)
    monkeypatch.setattr("greenlight.agent.which", lambda _n: "/usr/bin/pi")
    monkeypatch.setattr("greenlight.agent._last_assistant_text", lambda _o: "text")

    agent = Agent(deadline=Deadline(5))
    agent.run("prompt", cwd=".", timeout=1200)
    assert 0 < captured["timeout"] <= 5  # clamped down from 1200
