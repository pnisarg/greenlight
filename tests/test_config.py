from pathlib import Path

import pytest

from greenlight import config
from greenlight.util import GreenlightError


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
    # A reviewer with no explicit model inherits the run/review model.
    assert cfg.reviewers[0].model == ""


def test_load_per_reviewer_model(tmp_path: Path):
    (tmp_path / config.CONFIG_NAME).write_text(
        """
[[reviewers]]
name = "security"
focus = "security"
model = "openai-codex/gpt-5.5:high"

[[reviewers]]
name = "brutal"
focus = "bugs"
"""
    )
    cfg = config.load(tmp_path)
    by_name = {r.name: r for r in cfg.reviewers}
    assert by_name["security"].model == "openai-codex/gpt-5.5:high"
    assert by_name["brutal"].model == ""  # inherits the run/review model


# A reviewer's name is its identity: it keys the blocking-severity threshold map
# used to decide what blocks the gate, and labels findings and events. Two
# reviewers sharing a name silently resolve to one threshold, so a strict
# reviewer's verdict can be judged by a lax reviewer's threshold. Reject at load.
def _write_reviewers(tmp_path: Path, body: str) -> None:
    (tmp_path / config.CONFIG_NAME).write_text(body)


def test_load_rejects_duplicate_reviewer_names(tmp_path: Path):
    _write_reviewers(
        tmp_path,
        """
[[reviewers]]
name = "dup"
focus = "a"
blocking_severity = "error"

[[reviewers]]
name = "dup"
focus = "b"
blocking_severity = "info"
""",
    )
    with pytest.raises(GreenlightError, match="duplicate reviewer name 'dup'"):
        config.load(tmp_path)


def test_load_rejects_duplicate_after_whitespace_normalization(tmp_path: Path):
    """Names are compared stripped, so ' dup' and 'dup' are the same identity."""
    _write_reviewers(
        tmp_path,
        """
[[reviewers]]
name = "dup"
focus = "a"

[[reviewers]]
name = "  dup  "
focus = "b"
""",
    )
    with pytest.raises(GreenlightError, match="duplicate reviewer name"):
        config.load(tmp_path)


def test_load_rejects_duplicate_even_when_one_is_disabled(tmp_path: Path):
    """Disabled reviewers still occupy their name in the threshold map, so a
    collision with one is just as ambiguous."""
    _write_reviewers(
        tmp_path,
        """
[[reviewers]]
name = "dup"
focus = "a"

[[reviewers]]
name = "dup"
focus = "b"
enabled = false
""",
    )
    with pytest.raises(GreenlightError, match="duplicate reviewer name"):
        config.load(tmp_path)


@pytest.mark.parametrize("name", ['""', '"   "'])
def test_load_rejects_blank_reviewer_name(tmp_path: Path, name: str):
    """A blank name is not an identity: it can't be told apart from another
    blank one, and renders as an anonymous finding."""
    _write_reviewers(tmp_path, f"""
[[reviewers]]
name = {name}
focus = "a"
""")
    with pytest.raises(GreenlightError, match="reviewer name must not be empty"):
        config.load(tmp_path)


def test_load_rejects_reviewer_without_a_name(tmp_path: Path):
    """A missing name is reported as a config error, not a raw KeyError."""
    _write_reviewers(
        tmp_path,
        """
[[reviewers]]
focus = "a"
""",
    )
    with pytest.raises(GreenlightError, match="reviewer entry is missing"):
        config.load(tmp_path)


def test_load_rejects_non_table_reviewer_entry(tmp_path: Path):
    """`reviewers = ["brutal"]` is a config mistake, not a reviewer: report it
    instead of crashing on an attribute the entry doesn't have."""
    _write_reviewers(tmp_path, 'reviewers = ["brutal"]\n')
    with pytest.raises(GreenlightError, match="each reviewer must be a table"):
        config.load(tmp_path)


def test_load_keeps_distinct_reviewers_and_strips_names(tmp_path: Path):
    """Backward compatible: distinct reviewers still load, names are stored
    stripped so they match the identity that was uniqueness-checked."""
    _write_reviewers(
        tmp_path,
        """
[[reviewers]]
name = " brutal "
focus = "bugs"

[[reviewers]]
name = "security"
focus = "security"
enabled = false
""",
    )
    cfg = config.load(tmp_path)
    assert [r.name for r in cfg.reviewers] == ["brutal", "security"]
    assert [r.enabled for r in cfg.reviewers] == [True, False]
