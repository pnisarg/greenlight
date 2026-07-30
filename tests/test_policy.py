import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from greenlight import config, policy
from greenlight.cli import main
from greenlight.util import GreenlightError


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], work)
    _git(["config", "user.email", "t@t.com"], work)
    _git(["config", "user.name", "t"], work)
    (work / "README.md").write_text("# test\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "init"], work)
    return work


def _write_policy(repo: Path, *, severity: str = "warning", comment: str = "") -> None:
    (repo / config.CONFIG_NAME).write_text(
        f"""{comment}
[greenlight]
push_target = "origin"

[[reviewers]]
name = "security"
focus = "security"
blocking_severity = "{severity}"
"""
    )


def test_update_records_versioned_deterministic_effective_policy(repo):
    _write_policy(repo, comment="# first formatting")
    first = policy.update(repo)

    # Formatting and key order are not part of the effective policy identity.
    (repo / config.CONFIG_NAME).write_text(
        """
[[reviewers]]
focus = "security"
blocking_severity = "warning"
name = "security"

[greenlight]
push_target = "origin"
"""
    )
    second = policy.update(repo)

    assert first.digest == second.digest
    assert re.fullmatch(r"[0-9a-f]{64}", first.digest)
    assert first.version == 1

    payload = json.loads(first.path.read_text())
    assert payload["version"] == 1
    assert payload["policy"]["reviewers"][0]["name"] == "security"
    assert payload["policy"]["run_timeout"] == 1200  # omitted defaults are frozen


def test_policy_value_change_changes_digest(repo):
    _write_policy(repo, severity="warning")
    first = policy.update(repo)
    _write_policy(repo, severity="error")
    second = policy.update(repo)
    assert first.digest != second.digest


def test_feature_branch_config_does_not_change_trusted_policy(repo):
    _write_policy(repo, severity="warning")
    trusted = policy.update(repo)

    _write_policy(repo, severity="info")
    loaded = policy.load(repo)

    assert loaded.digest == trusted.digest
    assert loaded.config.reviewers[0].blocking_severity == "warning"


def test_missing_trusted_policy_fails_closed_with_migration_command(repo):
    with pytest.raises(GreenlightError, match=r"greenlight policy update"):
        policy.load(repo)


def test_migration_command_shell_quotes_repository_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    repo = tmp_path / "work repo"
    repo.mkdir()
    _git(["init"], repo)

    with pytest.raises(GreenlightError) as exc:
        policy.load(repo)
    assert f"--work '{repo}'" in str(exc.value)


def test_corrupt_snapshot_fails_closed(repo):
    _write_policy(repo)
    snapshot = policy.update(repo)
    snapshot.path.write_text("{}")

    with pytest.raises(GreenlightError, match="digest does not match"):
        policy.load(repo)


def test_failed_pointer_replace_preserves_previous_policy(repo, monkeypatch):
    _write_policy(repo, severity="warning")
    previous = policy.update(repo)
    _write_policy(repo, severity="error")

    real_replace = os.replace

    def fail_current(src, dst):
        if Path(dst).name == "current":
            raise OSError("simulated disk failure")
        real_replace(src, dst)

    monkeypatch.setattr(policy.os, "replace", fail_current)
    with pytest.raises(GreenlightError, match="could not update trusted policy"):
        policy.update(repo)

    loaded = policy.load(repo)
    assert loaded.digest == previous.digest
    assert loaded.config.reviewers[0].blocking_severity == "warning"
    assert not list(policy.policy_dir(repo).glob("*.tmp"))


def test_policy_update_cli_is_explicit_and_reports_digest(repo, capsys):
    _write_policy(repo)
    rc = main(["policy", "update", "--work", str(repo)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "trusted policy updated" in captured.err
    assert re.search(r"[0-9a-f]{64}", captured.err)


def test_policy_update_rejects_missing_repository_config(repo, capsys):
    rc = main(["policy", "update", "--work", str(repo)])
    captured = capsys.readouterr()

    assert rc == 2
    assert config.CONFIG_NAME in captured.err
    assert "create it or run `greenlight init`" in captured.err


def test_init_creates_initial_snapshot_but_reinit_does_not_update_it(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "work"
    repo.mkdir()
    _git(["init"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _write_policy(repo, severity="warning")

    assert main(["init", "--work", str(repo)]) == 0
    first = policy.load(repo)
    _write_policy(repo, severity="error")
    assert main(["init", "--work", str(repo)]) == 0
    second = policy.load(repo)
    capsys.readouterr()

    assert second.digest == first.digest
    assert second.config.reviewers[0].blocking_severity == "warning"


def test_init_does_not_implicitly_migrate_legacy_gate(tmp_path, monkeypatch, capsys):
    from greenlight import gate

    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    repo = tmp_path / "work"
    repo.mkdir()
    _git(["init"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _write_policy(repo, severity="info")
    gate.init(str(repo))  # simulate an installation predating policy snapshots

    assert main(["init", "--work", str(repo)]) == 0
    captured = capsys.readouterr()

    assert "greenlight policy update" in captured.err
    with pytest.raises(GreenlightError, match="no trusted policy snapshot"):
        policy.load(repo)


def test_run_missing_snapshot_stops_before_pipeline(repo, monkeypatch, capsys):
    monkeypatch.setattr("greenlight.cli.run_pipeline", lambda *_a, **_k: pytest.fail(
        "pipeline must not run without trusted policy"
    ))

    rc = main(["run", "--work", str(repo), "--intent", "test"])

    assert rc == 2
    assert "greenlight policy update" in capsys.readouterr().err
