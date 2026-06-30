"""CI-monitoring step: pure classification helpers + the poll/fix loop shell."""
from pathlib import Path

from greenlight import config, pipeline
from greenlight.config import default_config
from greenlight.steps import ci
from greenlight.steps.types import StepResult


# --- pure helpers ----------------------------------------------------------


def _checkrun(name, status, conclusion=""):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def test_normalize_checkrun_and_statuscontext():
    rollup = [
        _checkrun("lint-test", "COMPLETED", "SUCCESS"),
        _checkrun("build", "IN_PROGRESS"),
        {"__typename": "StatusContext", "context": "legacy/ci", "state": "FAILURE"},
    ]
    norm = ci._normalize_rollup(rollup)
    assert {c["name"] for c in norm} == {"lint-test", "build", "legacy/ci"}
    lint = next(c for c in norm if c["name"] == "lint-test")
    assert lint["done"] and lint["ok"] and not lint["failed"]
    build = next(c for c in norm if c["name"] == "build")
    assert not build["done"]
    legacy = next(c for c in norm if c["name"] == "legacy/ci")
    assert legacy["done"] and legacy["failed"] and not legacy["ok"]


def test_classify_pending_then_pass_then_fail():
    running = ci._normalize_rollup([_checkrun("t", "IN_PROGRESS")])
    assert ci._classify(running, [])[0] == "pending"

    passing = ci._normalize_rollup([_checkrun("t", "COMPLETED", "SUCCESS")])
    assert ci._classify(passing, [])[0] == "pass"

    verdict, _summary, failed = ci._classify(
        ci._normalize_rollup([_checkrun("unit", "COMPLETED", "FAILURE")]), []
    )
    assert verdict == "fail"
    assert failed == ["unit"]


def test_classify_empty_rollup():
    assert ci._classify([], [])[0] == "empty"


def test_classify_neutral_and_skipped_count_as_pass():
    checks = ci._normalize_rollup([
        _checkrun("a", "COMPLETED", "NEUTRAL"),
        _checkrun("b", "COMPLETED", "SKIPPED"),
    ])
    assert ci._classify(checks, [])[0] == "pass"


def test_required_checks_filter_and_missing():
    checks = ci._normalize_rollup([
        _checkrun("lint-test", "COMPLETED", "SUCCESS"),
        _checkrun("flaky-extra", "COMPLETED", "FAILURE"),
    ])
    # Only gate on lint-test → the unrelated failing check is ignored.
    assert ci._classify(checks, ["lint-test"])[0] == "pass"
    # A required check that hasn't reported yet keeps us pending.
    assert ci._classify(checks, ["docker-build"])[0] == "pending"


def test_interval_backoff():
    assert ci._interval(0) == 30
    assert ci._interval(301) == 60
    assert ci._interval(1000) == 120


# --- config ----------------------------------------------------------------


def test_config_ci_defaults_off():
    cfg = default_config()
    assert cfg.ci_enabled is False
    assert cfg.ci_provider == "github"
    assert cfg.ci_max_fix_rounds == 2


def test_config_loads_ci_block(tmp_path: Path):
    (tmp_path / config.CONFIG_NAME).write_text(
        """
[ci]
enabled = true
timeout = 1800
max_fix_rounds = 1
required_checks = ["lint-test", "docker-build"]
"""
    )
    cfg = config.load(tmp_path)
    assert cfg.ci_enabled is True
    assert cfg.ci_timeout == 1800
    assert cfg.ci_max_fix_rounds == 1
    assert cfg.ci_required_checks == ["lint-test", "docker-build"]


# --- run_step shell --------------------------------------------------------


class _FakeAgent:
    def __init__(self, *a, **k):
        self.calls = 0

    def run(self, *a, **k):
        self.calls += 1
        return None


def _patch_common(monkeypatch):
    monkeypatch.setattr(ci, "which", lambda _t: "/usr/bin/gh")
    monkeypatch.setattr(ci, "Agent", _FakeAgent)
    monkeypatch.setattr(ci.time, "sleep", lambda *_a: None)


def test_run_step_skips_when_gh_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "which", lambda _t: None)
    res = ci.run_step(str(tmp_path), default_config(), "feat/x", "intent",
                      lambda m: False, lambda: True, pr_skipped=False)
    assert res.skipped and res.passed


def test_run_step_skips_when_no_pr(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    res = ci.run_step(str(tmp_path), default_config(), "feat/x", "intent",
                      lambda m: False, lambda: True, pr_skipped=True)
    assert res.skipped and res.passed


def test_run_step_passes_on_green(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    green = ci._normalize_rollup([_checkrun("lint-test", "COMPLETED", "SUCCESS")])
    monkeypatch.setattr(ci, "_fetch_rollup", lambda *_a: green)
    fixed = {"n": 0}

    def commit(_m):
        fixed["n"] += 1
        return True

    res = ci.run_step(str(tmp_path), default_config(), "feat/x", "intent",
                      commit, lambda: True, pr_skipped=False)
    assert res.passed and not res.skipped
    assert fixed["n"] == 0  # no fix attempted on a green PR


def test_run_step_fixes_then_passes(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    # Red on the first poll, green forever after the fix re-push (green must
    # repeat: a pass is only trusted after _GREEN_STABLE consecutive polls).
    state = {"polls": 0}
    red = ci._normalize_rollup([_checkrun("unit", "COMPLETED", "FAILURE")])
    green = ci._normalize_rollup([_checkrun("unit", "COMPLETED", "SUCCESS")])

    def fetch(*_a):
        state["polls"] += 1
        return red if state["polls"] == 1 else green

    monkeypatch.setattr(ci, "_fetch_rollup", fetch)
    monkeypatch.setattr(ci, "_failed_logs", lambda *_a: "boom: assertion failed")
    pushes = {"n": 0}

    res = ci.run_step(str(tmp_path), default_config(), "feat/x", "intent",
                      lambda m: True, lambda: pushes.__setitem__("n", pushes["n"] + 1) or True,
                      pr_skipped=False)
    assert res.passed
    assert pushes["n"] == 1  # one fix re-push


def test_run_step_fails_when_fix_budget_exhausted(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    red = ci._normalize_rollup([_checkrun("unit", "COMPLETED", "FAILURE")])
    monkeypatch.setattr(ci, "_fetch_rollup", lambda *_a: red)
    cfg = default_config()
    cfg.ci_max_fix_rounds = 0  # no fixes allowed → fail immediately
    res = ci.run_step(str(tmp_path), cfg, "feat/x", "intent",
                      lambda m: True, lambda: True, pr_skipped=False)
    assert not res.passed and not res.skipped
    assert any(f.description.startswith("CI check failed") for f in res.findings)


def test_run_step_fails_when_fix_makes_no_change(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    red = ci._normalize_rollup([_checkrun("unit", "COMPLETED", "FAILURE")])
    monkeypatch.setattr(ci, "_fetch_rollup", lambda *_a: red)
    monkeypatch.setattr(ci, "_failed_logs", lambda *_a: "")
    # commit_fn returns False → agent produced no changes → stop, don't loop.
    res = ci.run_step(str(tmp_path), default_config(), "feat/x", "intent",
                      lambda m: False, lambda: True, pr_skipped=False)
    assert not res.passed
    assert "no changes" in res.summary


# --- pipeline wiring -------------------------------------------------------


def _stub_pipeline(monkeypatch, calls):
    monkeypatch.setattr(pipeline.gitx, "rev_parse", lambda *a, **k: "HEAD_SHA")
    monkeypatch.setattr(pipeline, "_resolve_base", lambda *a, **k: "BASE_SHA")
    monkeypatch.setattr(pipeline.gitx, "changed_files", lambda *a, **k: ["a.py"])
    monkeypatch.setattr(pipeline.intent_step, "capture", lambda *a, **k: "intent")
    monkeypatch.setattr(pipeline, "Agent", lambda *a, **k: object())
    monkeypatch.setattr(pipeline.lint_step, "run_step",
                        lambda *a, **k: StepResult(name="lint", passed=True, skipped=True))
    monkeypatch.setattr(pipeline.review_step, "run_step",
                        lambda *a, **k: StepResult(name="review", passed=True))
    monkeypatch.setattr(pipeline.verify_step, "run_step",
                        lambda *a, **k: [StepResult(name="verify-backend", passed=True)])
    monkeypatch.setattr(pipeline.pr_step, "run_step",
                        lambda *a, **k: calls.append("pr") or StepResult(name="pr", passed=True))


def test_ci_runs_after_pr_when_enabled(monkeypatch, tmp_path):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, calls)
    monkeypatch.setattr(pipeline.ci_step, "run_step",
                        lambda *a, **k: calls.append("ci") or StepResult(name="ci", passed=True))

    cfg = default_config()
    cfg.ci_enabled = True
    passed = pipeline.run_pipeline(str(tmp_path), cfg, "feat/x", "BASE_SHA", "main",
                                   "intent", lambda: calls.append("forward") or True)
    assert passed is True
    assert calls == ["forward", "pr", "ci"], calls


def test_ci_skipped_when_disabled(monkeypatch, tmp_path):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, calls)
    monkeypatch.setattr(pipeline.ci_step, "run_step",
                        lambda *a, **k: calls.append("ci") or StepResult(name="ci", passed=True))

    cfg = default_config()  # ci disabled by default
    passed = pipeline.run_pipeline(str(tmp_path), cfg, "feat/x", "BASE_SHA", "main",
                                   "intent", lambda: calls.append("forward") or True)
    assert passed is True
    assert "ci" not in calls


def test_ci_failure_fails_the_gate(monkeypatch, tmp_path):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, calls)
    monkeypatch.setattr(pipeline.ci_step, "run_step",
                        lambda *a, **k: StepResult(name="ci", passed=False, summary="CI red"))

    cfg = default_config()
    cfg.ci_enabled = True
    passed = pipeline.run_pipeline(str(tmp_path), cfg, "feat/x", "BASE_SHA", "main",
                                   "intent", lambda: True)
    assert passed is False
