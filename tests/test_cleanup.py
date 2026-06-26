"""Disk hygiene: orphaned worktree sweep and bare-repo gc."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from greenlight import gate, worktree


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def gated_repo(tmp_path, monkeypatch):
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
    res = gate.init(str(work))
    return work, Path(res["bare"])


def test_sweep_removes_stale_but_keeps_fresh(gated_repo, tmp_path):
    _work, bare = gated_repo
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()

    stale = tmp_root / "greenlight-wt-old"
    stale.mkdir()
    (stale / "junk").write_text("x")
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))

    fresh = tmp_root / "greenlight-wt-new"
    fresh.mkdir()
    (fresh / "junk").write_text("x")

    unrelated = tmp_root / "something-else"
    unrelated.mkdir()

    worktree.sweep_stale(bare, tmp_root=tmp_root)

    assert not stale.exists(), "stale orphaned worktree dir should be swept"
    assert fresh.exists(), "fresh dir (possible live run) must be left alone"
    assert unrelated.exists(), "non-greenlight dirs must never be touched"


def test_checkout_still_cleans_up_its_own_dir(gated_repo):
    work, bare = gated_repo
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work,
                            capture_output=True, text=True).stdout.strip()
    # Stage the branch + objects into the bare repo (a real push/fetch would).
    subprocess.run(["git", "fetch", "--force", str(work),
                    f"refs/heads/{branch}:refs/heads/{branch}"], cwd=bare,
                   check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", branch], cwd=bare,
                          capture_output=True, text=True).stdout.strip()
    with worktree.checkout(bare, "feat/x", head) as wt:
        assert Path(wt).is_dir()
        base = Path(wt).parent
    assert not base.exists(), "worktree temp dir must be removed on clean exit"


def test_gc_bare_compacts_and_reports(gated_repo):
    _work, bare = gated_repo
    before, after = gate.gc_bare(bare)
    assert before >= 0 and after >= 0
    assert (bare / "HEAD").exists(), "gc must not destroy the repo"


def test_list_bare_repos_finds_provisioned(gated_repo):
    _work, bare = gated_repo
    repos = gate.list_bare_repos()
    assert bare in repos


def test_gc_cli_all(gated_repo):
    _work, _bare = gated_repo
    res = subprocess.run(
        ["python", "-m", "greenlight", "gc", "--all"],
        capture_output=True, text=True, env={**os.environ},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "reclaimed" in res.stderr
