"""The structured event emitter: off by default, JSONL when GREENLIGHT_EVENTS is set."""
import json

import pytest

from greenlight import events


@pytest.fixture(autouse=True)
def _reset_sink():
    """events caches its sink in a module global; reset around each test."""
    events._sink = None
    events._mirror = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    if events._mirror is not None:
        events._mirror.close()
    events._sink = None
    events._mirror = None


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


def test_enable_default_mirrors_when_caller_owns_primary_stream(monkeypatch, tmp_path):
    """When a caller (the pi extension) sets GREENLIGHT_EVENTS to its own file,
    enable_default mirrors events to the per-repo default path too, so
    `greenlight watch` can render the run."""
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    primary = tmp_path / "caller.jsonl"
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(primary))

    repo = str(tmp_path / "repo")
    active = events.enable_default(repo)
    # Caller's primary stream still wins (its path is returned, env untouched).
    assert active == str(primary)

    events.emit("run_start", branch="feat/x")
    events.emit("run_end", passed=True)

    default = events.default_path(repo)
    # Both the caller's file and the per-repo mirror got every event.
    assert len(primary.read_text().splitlines()) == 2
    assert len(default.read_text().splitlines()) == 2


def test_enable_default_does_not_mirror_when_env_is_the_default_path(monkeypatch, tmp_path):
    """If GREENLIGHT_EVENTS already points at the per-repo default path, don't
    open a redundant self-mirror (which would double every line)."""
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    repo = str(tmp_path / "repo")
    default = events.default_path(repo)
    default.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(default))

    events.enable_default(repo)
    assert events._mirror is None
    events.emit("run_start", branch="feat/x")
    assert len(default.read_text().splitlines()) == 1


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
