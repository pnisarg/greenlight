"""The review-log: archive past runs' findings and render them on demand.

The PR deliberately carries no findings; `greenlight review-log` is the way to
inspect what the reviewers flagged after a run. These tests cover the detailed
report renderer, the per-run event archival, and the CLI's run selection.
"""
import json
import subprocess

import pytest

from greenlight import events
from greenlight.cli import main


def _events(*recs) -> str:
    return "\n".join(json.dumps(r) for r in recs)


RUN_WITH_FINDINGS = _events(
    {"ts": 1, "type": "run_start", "branch": "feat/x", "classification": "backend",
     "files": ["a.py", "b.py"]},
    {"ts": 2, "type": "intent", "source": "supplied", "text": "do a thing"},
    {"ts": 3, "type": "review_round", "round": 1, "max_rounds": 3},
    {"ts": 4, "type": "reviewer", "name": "brutal", "round": 1, "findings": None, "blocking": None},
    {"ts": 5, "type": "reviewer", "name": "brutal", "round": 1, "findings": 2, "blocking": 1,
     "items": [
         {"severity": "error", "file": "a.py", "line": 10, "description": "null deref", "blocks": True},
         {"severity": "info", "file": "b.py", "line": None, "description": "minor nit", "blocks": False},
     ]},
    {"ts": 6, "type": "reviewer", "name": "security", "round": 1, "findings": 0, "blocking": 0, "items": []},
    {"ts": 7, "type": "fix", "round": 1, "findings": 1},
    {"ts": 8, "type": "review_round", "round": 2, "max_rounds": 3},
    {"ts": 9, "type": "reviewer", "name": "brutal", "round": 2, "findings": 0, "blocking": 0, "items": []},
    {"ts": 10, "type": "reviewer", "name": "security", "round": 2, "findings": 0, "blocking": 0, "items": []},
    {"ts": 11, "type": "run_end", "passed": True},
)


def test_report_lists_every_finding_per_round():
    from greenlight import render

    rows = render.render_review_log(RUN_WITH_FINDINGS, color=False)
    text = "\n".join(rows)
    assert "round 1" in text
    assert "round 2" in text
    assert "brutal: 2 findings, 1 blocking" in text
    assert "a.py:10 [blocking] — null deref" in text
    assert "b.py — minor nit" in text  # line=None -> no :line suffix
    assert "PASSED — 2 findings, 1 blocking" in text


def test_report_handles_missing_item_detail():
    """Older runs (or the 'started' event) lack item detail; don't crash."""
    from greenlight import render

    text = _events(
        {"type": "run_start", "branch": "feat/y", "classification": "backend", "files": ["a.py"]},
        {"type": "review_round", "round": 1, "max_rounds": 3},
        {"type": "reviewer", "name": "brutal", "round": 1, "findings": 3, "blocking": 2},
        {"type": "run_end", "passed": False},
    )
    rows = render.render_review_log(text, color=False)
    joined = "\n".join(rows)
    assert "brutal: 3 findings, 2 blocking" in joined
    assert "detail unavailable" in joined
    assert "FAILED" in joined


def test_report_empty_stream():
    from greenlight import render

    assert "no review activity" in "\n".join(render.render_review_log("", color=False))


@pytest.fixture(autouse=True)
def _reset_sink():
    events._sink = None
    events._mirror = None
    yield
    if events._sink not in (None, False):
        events._sink.close()
    if events._mirror is not None:
        events._mirror.close()
    events._sink = None
    events._mirror = None


def test_enable_default_archives_prior_run(monkeypatch, tmp_path):
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    monkeypatch.delenv("GREENLIGHT_EVENTS", raising=False)
    repo = str(tmp_path / "repo")

    events.enable_default(repo)
    events.emit("run_start", branch="feat/a")
    events.emit("run_end", passed=True)
    events._sink.close()
    events._sink = None

    events.enable_default(repo)  # truncates live -> must archive the first run
    events.emit("run_start", branch="feat/b")
    events.emit("run_end", passed=False)
    events._sink.close()
    events._sink = None

    logs = events.run_logs(repo)
    assert len(logs) == 2
    # Newest (live) first, then the archived prior run.
    assert "feat/b" in logs[0].read_text()
    assert "feat/a" in logs[1].read_text()
    assert events.history_dir(repo).exists()


def test_archive_retention_caps_history(monkeypatch, tmp_path):
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(events, "_HISTORY_KEEP", 3)
    monkeypatch.delenv("GREENLIGHT_EVENTS", raising=False)
    repo = str(tmp_path / "repo")

    for i in range(6):
        events.enable_default(repo)
        events.emit("run_start", branch=f"feat/{i}")
        events.emit("run_end", passed=True)
        events._sink.close()
        events._sink = None

    archived = list(events.history_dir(repo).glob("*.jsonl"))
    assert len(archived) == 3  # capped; live events.jsonl holds the 6th run


def _init_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for args in (["init"], ["config", "user.email", "t@t.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True, capture_output=True)
    return work


def test_cli_review_log_no_runs(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    work = _init_repo(tmp_path)
    rc = main(["review-log", "--work", str(work)])
    assert rc == 1
    assert "no runs recorded" in capsys.readouterr().err


def test_cli_review_log_shows_latest_and_lists(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("greenlight.util.state_dir", lambda: tmp_path / "state")
    work = _init_repo(tmp_path)
    events.default_path(str(work)).parent.mkdir(parents=True, exist_ok=True)
    events.default_path(str(work)).write_text(RUN_WITH_FINDINGS)

    rc = main(["review-log", "--work", str(work)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "null deref" in out

    rc = main(["review-log", "--work", str(work), "--list"])
    assert rc == 0
    assert "feat/x" in capsys.readouterr().err

    rc = main(["review-log", "--work", str(work), "--run", "9"])
    assert rc == 1
    assert "out of range" in capsys.readouterr().err
