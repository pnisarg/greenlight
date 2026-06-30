from pathlib import Path

from greenlight import config


def test_defaults_have_brutal_and_security():
    cfg = config.default_config()
    names = {r.name for r in cfg.reviewers}
    assert {"brutal", "security"} <= names


def test_load_missing_returns_defaults(tmp_path: Path):
    cfg = config.load(tmp_path)
    assert cfg.max_review_rounds == 3
    assert cfg.push_target == "origin"
    assert cfg.review_model == ""


def test_load_overrides(tmp_path: Path):
    (tmp_path / config.CONFIG_NAME).write_text(
        """
[greenlight]
max_review_rounds = 5
model = "anthropic/claude-sonnet-4"
review_model = "openai-codex/gpt-5.5:high"

[checks]
lint_cmd = "ruff check ."

[[reviewers]]
name = "perf"
focus = "performance only"
blocking_severity = "error"

[[verify.backend]]
name = "unit"
cmd = "pytest -q"

[verify.frontend]
server_cmd = "npm run dev"
url = "http://localhost:5173"

[routing]
backend = ["*.py"]
"""
    )
    cfg = config.load(tmp_path)
    assert cfg.max_review_rounds == 5
    assert cfg.model == "anthropic/claude-sonnet-4"
    assert cfg.review_model == "openai-codex/gpt-5.5:high"
    assert cfg.lint_cmd == "ruff check ."
    assert [r.name for r in cfg.reviewers] == ["perf"]
    assert cfg.reviewers[0].blocking_severity == "error"
    assert cfg.verify_backend[0].cmd == "pytest -q"
    assert cfg.frontend_server_cmd == "npm run dev"
    assert cfg.frontend_url == "http://localhost:5173"
    assert cfg.routing.backend == ["*.py"]
