"""The structured event emitter: off by default, JSONL when GREENLIGHT_EVENTS is set."""
import json

import pytest

from greenlight import events


@pytest.fixture(autouse=True)
def _reset_sink():
    """events caches its sink in a module global; reset around each test."""
    events._sink = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    events._sink = None


def test_emit_is_noop_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("GREENLIGHT_EVENTS", raising=False)
    events.emit("run_start", branch="x")
    # Nothing to assert beyond "did not raise / did not create a file"; the sink
    # short-circuits to False.
    assert events._sink is False


def test_emit_writes_jsonl_when_enabled(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))

    events.emit("run_start", branch="feat/x", classification="backend", files=["a.py"])
    events.emit("reviewer", name="brutal", round=1, findings=2, blocking=1)
    events.emit("run_end", passed=True)

    lines = path.read_text().splitlines()
    assert len(lines) == 3
    recs = [json.loads(line) for line in lines]
    assert [r["type"] for r in recs] == ["run_start", "reviewer", "run_end"]
    assert all("ts" in r for r in recs)
    assert recs[0]["files"] == ["a.py"]
    assert recs[1]["blocking"] == 1
    assert recs[2]["passed"] is True


def test_emit_disables_gracefully_on_bad_path(monkeypatch, tmp_path):
    # A path whose parent does not exist can't be opened; emit must not raise.
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(tmp_path / "nope" / "events.jsonl"))
    events.emit("run_start", branch="x")
    assert events._sink is False


def test_claiming_sink_scrubs_env_so_children_dont_inherit(monkeypatch, tmp_path):
    """Regression: once a process opens the sink, GREENLIGHT_EVENTS is removed
    from os.environ so child subprocesses (lint/verify, agent pi calls, nested
    greenlight runs) don't inherit it and append to our stream."""
    import os

    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))
    events.emit("run_start", branch="x")
    # The env var is gone, but our handle stays open and writes still land.
    assert "GREENLIGHT_EVENTS" not in os.environ
    events.emit("run_end", passed=True)
    assert len(path.read_text().splitlines()) == 2
