import subprocess
from pathlib import Path

import pytest

from greenlight import gate, gitx


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], work)
    _git(["config", "user.email", "t@t.com"], work)
    _git(["config", "user.name", "t"], work)
    _git(["remote", "add", "origin", str(origin)], work)
    (work / "a.txt").write_text("hi\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "init"], work)
    return work


def test_init_creates_gate_and_remote(repo):
    res = gate.init(str(repo))
    assert Path(res["bare"]).joinpath("HEAD").exists()
    assert gitx.remote_url(repo, gate.REMOTE_NAME) == res["bare"]
    hook = Path(res["bare"]) / "hooks" / "post-receive"
    assert hook.exists()
    assert hook.stat().st_mode & 0o111  # executable


def test_init_idempotent(repo):
    a = gate.init(str(repo))
    b = gate.init(str(repo))
    assert a["bare"] == b["bare"]
    assert gitx.remote_url(repo, gate.REMOTE_NAME) == b["bare"]


def test_init_requires_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "ghome"))
    work = tmp_path / "noorigin"
    work.mkdir()
    _git(["init"], work)
    with pytest.raises(Exception):
        gate.init(str(work))
