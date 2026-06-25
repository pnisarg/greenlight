"""End-to-end smoke: push through the gate, pipeline runs in a worktree,
clean change is forwarded to origin.

Uses a fake `pi` on PATH so no real LLM/network is needed. gh is absent in the
worktree's PATH override, so the PR step is skipped (still passes the gate).
"""
import os
import subprocess
from pathlib import Path

import pytest


def _git(args, cwd, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ghome = tmp_path / "ghome"
    monkeypatch.setenv("GREENLIGHT_HOME", str(ghome))

    # Fake pi shim on PATH (gh deliberately not shadowed; the worktree may or
    # may not have a real gh — PR step is idempotent/skips gracefully).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = Path(__file__).parent / "fake_pi.py"
    shim = bindir / "pi"
    shim.write_text(f'#!/usr/bin/env bash\nexec python3 "{fake}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "t@t.com"], work)
    _git(["config", "user.name", "t"], work)
    _git(["remote", "add", "origin", str(origin)], work)
    (work / "README.md").write_text("# proj\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "init"], work)
    _git(["push", "origin", "main"], work)
    return tmp_path, work, origin


def _origin_has_branch(origin: Path, branch: str) -> bool:
    out = subprocess.run(["git", "branch", "--list", branch], cwd=origin,
                         capture_output=True, text=True)
    return branch in out.stdout


def test_push_through_gate_forwards_clean_change(env):
    from greenlight import gate

    tmp_path, work, origin = env
    gate.init(str(work))

    # Feature branch with a clean backend change.
    _git(["checkout", "-b", "feat/add-greeting"], work)
    (work / "greet.py").write_text("def greet(name):\n    return f'hi {name}'\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "feat: add greeting helper"], work)

    # Push through the gate with an explicit intent push option.
    res = subprocess.run(
        ["git", "push", "-o", "intent=Add a greeting helper function",
         gate.REMOTE_NAME, "feat/add-greeting"],
        cwd=work, capture_output=True, text=True,
    )
    # The hook runs the pipeline; on pass it forwards to origin.
    assert res.returncode == 0, res.stderr
    assert _origin_has_branch(origin, "feat/add-greeting"), res.stderr


def test_greenlight_run_forwards_clean_change(env):
    """The explicit `greenlight run` path validates in a worktree off the bare
    repo and forwards the result to origin (regression: it must run the pipeline
    once and forward from the bare repo, not the root)."""
    from greenlight import gate

    tmp_path, work, origin = env
    gate.init(str(work))

    _git(["checkout", "-b", "feat/run-path"], work)
    (work / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "feat: add module"], work)

    res = subprocess.run(
        ["python", "-m", "greenlight", "run", "--intent", "Add an add() helper"],
        cwd=work, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _origin_has_branch(origin, "feat/run-path"), res.stdout + res.stderr


def test_greenlight_run_emits_event_stream(env):
    """With GREENLIGHT_EVENTS set, a full passing run writes a structured JSONL
    stream from run_start through run_end (the handoff contract the UI renders)."""
    import json

    from greenlight import gate

    tmp_path, work, origin = env
    gate.init(str(work))

    _git(["checkout", "-b", "feat/events"], work)
    (work / "calc.py").write_text("def mul(a, b):\n    return a * b\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "feat: add mul"], work)

    events_path = tmp_path / "events.jsonl"
    res = subprocess.run(
        ["python", "-m", "greenlight", "run", "--intent", "Add a mul() helper"],
        cwd=work, capture_output=True, text=True,
        env={**os.environ, "GREENLIGHT_EVENTS": str(events_path)},
    )
    assert res.returncode == 0, res.stdout + res.stderr

    recs = [json.loads(line) for line in events_path.read_text().splitlines()]
    types = [r["type"] for r in recs]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"
    assert recs[-1]["passed"] is True
    # Core stages all show up.
    for t in ("intent", "lint", "review_round", "reviewer", "verify"):
        assert t in types, (t, types)
    # Intent was supplied, not reconstructed.
    intent_ev = next(r for r in recs if r["type"] == "intent")
    assert intent_ev["source"] == "supplied"
    # Classification is backend for a lone .py change.
    assert recs[0]["classification"] == "backend"
