"""`greenlight watch` must not spin forever on a run that was killed mid-flight.

When the pi window closes it kills the spawned `greenlight run`, so the event
stream stops at a `reviewer ... started` line with no `run_end`. The watcher
stamps the gate PID into run_start; once the stream goes idle past the grace
window and that PID is gone, watch declares the run abandoned and exits non-zero
instead of polling a corpse.
"""
import itertools
import json
import os

from greenlight import cli


def _advancing_clock(step: float = 1000.0):
    """A monotonic() stand-in that jumps forward `step` seconds each call, so
    grace/timeout windows elapse deterministically without real sleeping."""
    counter = itertools.count(0)
    return lambda: next(counter) * step


def _stuck_stream(pid: int) -> str:
    return "\n".join(json.dumps(e) for e in [
        {"ts": 1, "type": "run_start", "branch": "feat/x", "classification": "backend",
         "files": ["a.py"], "pid": pid},
        {"ts": 2, "type": "intent", "source": "supplied", "text": "x"},
        {"ts": 3, "type": "lint", "status": "pass", "fixed": False},
        {"ts": 4, "type": "review_round", "round": 1, "max_rounds": 3},
        {"ts": 5, "type": "reviewer", "name": "brutal", "round": 1, "findings": None, "blocking": None},
    ])


def _dead_pid() -> int:
    """A PID that is (almost certainly) not running."""
    pid = os.fork() if hasattr(os, "fork") else None
    if pid == 0:  # pragma: no cover - child exits immediately
        os._exit(0)
    if pid:
        os.waitpid(pid, 0)
        return pid
    return 2**31 - 1  # fallback: implausibly high pid


def test_pipeline_alive_detects_dead_and_unknown():
    assert cli._pipeline_alive(None) is True  # unknown -> assume alive, never falsely abandon
    assert cli._pipeline_alive(os.getpid()) is True
    assert cli._pipeline_alive(_dead_pid()) is False


def test_watch_abandons_dead_run(monkeypatch, tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    path.write_text(_stuck_stream(_dead_pid()))
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))
    monkeypatch.setattr("greenlight.gitx.main_repo_root", lambda *a, **k: str(tmp_path))

    # No real waiting: the clock jumps 1000s per call so the idle window blows
    # past --grace immediately, and sleeps are skipped.
    monkeypatch.setattr(cli.time, "monotonic", _advancing_clock())
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    rc = cli.main(["watch", "--work", str(tmp_path), "--interval", "0", "--grace", "120"])
    assert rc == 3
    assert "abandoned" in capsys.readouterr().err


def test_watch_does_not_abandon_when_pid_alive(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(_stuck_stream(os.getpid()))  # our own pid: very much alive
    monkeypatch.setenv("GREENLIGHT_EVENTS", str(path))
    monkeypatch.setattr("greenlight.gitx.main_repo_root", lambda *a, **k: str(tmp_path))

    monkeypatch.setattr(cli.time, "monotonic", _advancing_clock())
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    # Alive pid + a --timeout so the loop still terminates (via the timeout path,
    # not the abandon path). Returns 2 (timeout), proving abandon didn't fire.
    rc = cli.main(["watch", "--work", str(tmp_path), "--interval", "0",
                   "--grace", "120", "--timeout", "1"])
    assert rc == 2
