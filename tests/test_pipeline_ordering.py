"""Regression: the branch is forwarded to the remote BEFORE the PR is opened.

`gh pr create --head <branch>` needs the branch to exist on origin. Bug found by
dogfooding: run_pipeline opened the PR while cli.py forwarded only afterwards, so
PR creation failed with "No commits between main and <branch>".
"""
from greenlight import pipeline
from greenlight.config import default_config
from greenlight.steps.types import StepResult


def test_forward_runs_before_pr(monkeypatch, tmp_path):
    calls: list[str] = []

    # Stub every step so no real agent/git runs; record call order.
    monkeypatch.setattr(pipeline.gitx, "rev_parse", lambda *a, **k: "HEAD_SHA")
    monkeypatch.setattr(pipeline, "_resolve_base", lambda *a, **k: "BASE_SHA")
    monkeypatch.setattr(pipeline.gitx, "changed_files", lambda *a, **k: ["a.py"])
    monkeypatch.setattr(pipeline.intent_step, "capture", lambda *a, **k: "do a thing")
    monkeypatch.setattr(pipeline, "Agent", lambda *a, **k: object())

    monkeypatch.setattr(
        pipeline.lint_step, "run_step",
        lambda *a, **k: StepResult(name="lint", passed=True, skipped=True),
    )
    monkeypatch.setattr(
        pipeline.review_step, "run_step",
        lambda *a, **k: StepResult(name="review", passed=True),
    )
    monkeypatch.setattr(
        pipeline.verify_step, "run_step",
        lambda *a, **k: [StepResult(name="verify-backend", passed=True)],
    )

    def fake_pr(*a, **k):
        calls.append("pr")
        return StepResult(name="pr", passed=True, summary="https://x/y/pull/1")

    monkeypatch.setattr(pipeline.pr_step, "run_step", fake_pr)

    def forward():
        calls.append("forward")
        return True

    passed = pipeline.run_pipeline(
        str(tmp_path), default_config(), "feat/x", "BASE_SHA", "main", "intent", forward
    )

    assert passed is True
    assert calls == ["forward", "pr"], calls


def test_forward_failure_aborts_before_pr(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(pipeline.gitx, "rev_parse", lambda *a, **k: "HEAD_SHA")
    monkeypatch.setattr(pipeline, "_resolve_base", lambda *a, **k: "BASE_SHA")
    monkeypatch.setattr(pipeline.gitx, "changed_files", lambda *a, **k: ["a.py"])
    monkeypatch.setattr(pipeline.intent_step, "capture", lambda *a, **k: "x")
    monkeypatch.setattr(pipeline, "Agent", lambda *a, **k: object())
    monkeypatch.setattr(pipeline.lint_step, "run_step",
                        lambda *a, **k: StepResult(name="lint", passed=True, skipped=True))
    monkeypatch.setattr(pipeline.review_step, "run_step",
                        lambda *a, **k: StepResult(name="review", passed=True))
    monkeypatch.setattr(pipeline.verify_step, "run_step",
                        lambda *a, **k: [StepResult(name="verify-backend", passed=True)])
    monkeypatch.setattr(pipeline.pr_step, "run_step",
                        lambda *a, **k: calls.append("pr") or StepResult(name="pr", passed=True))

    passed = pipeline.run_pipeline(
        str(tmp_path), default_config(), "feat/x", "BASE_SHA", "main", "intent",
        lambda: False,  # forward fails
    )

    assert passed is False
    assert "pr" not in calls  # PR never attempted when forward fails
