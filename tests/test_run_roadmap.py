"""The roadmap runner's contract: read GitHub, run one issue, verify, stop.

Every test drives the real orchestrator through a fake subprocess boundary, so
the assertions are about argv and control flow -- no network, no git, no pi.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "automation" / "run_roadmap.py"
_SPEC = importlib.util.spec_from_file_location("greenlight_run_roadmap", _PATH)
assert _SPEC and _SPEC.loader
rr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rr
_SPEC.loader.exec_module(rr)


def body(*, dependency: str = "None", branch: str = "fix/thing",
         pr_title: str = "fix(verify): fail closed") -> str:
    return (
        "## Intent\nDo the thing.\n\n"
        "## Delivery\n"
        f"- Dependency: {dependency}\n"
        f"- Branch: `{branch}`\n"
        f"- Draft PR title: `{pr_title}`\n"
    )


def issue_json(number, *, state="OPEN", labels=("roadmap",), title=None, **kw):
    return {
        "number": number,
        "title": title if title is not None else f"GL-{number:03d}: Task {number}",
        "state": state,
        "labels": [{"name": name} for name in labels],
        "body": body(**kw),
    }


class FakeRunner:
    """Records argv; replies from a prefix-matched script of canned results."""

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.stream_code = 0
        self.logs: list[Path | None] = []

    def _reply(self, args):
        for prefix, value in self.script.items():
            if args[: len(prefix)] == list(prefix):
                return value
        return ""

    def run(self, argv, *, cwd=None, timeout=None, check=True):
        args = [str(a) for a in argv]
        rr._forbid(args)
        self.calls.append(args)
        reply = self._reply(args)
        if isinstance(reply, rr.Result):
            result = rr.Result(args, reply.code, reply.out, reply.err)
        else:
            out = reply if isinstance(reply, str) else json.dumps(reply)
            result = rr.Result(args, 0, out, "")
        if check and not result.ok:
            raise rr.RunnerError(f"command failed ({result.code})")
        return result

    def stream(self, argv, *, cwd, timeout, log_path):
        args = [str(a) for a in argv]
        rr._forbid(args)
        self.streamed.append(args)
        self.logs.append(log_path)
        return self.stream_code

    # -- assertions helpers --

    def ran(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def orch(runner, **kw):
    return rr.Orchestrator(runner, repo="acme/proj", root=Path("/repo"), **kw)


# --------------------------------------------------------------- issue contract


def test_delivery_block_is_the_build_contract():
    issue = rr.parse_issue(issue_json(23, dependency="#22"))
    assert issue.branch == "fix/thing"
    assert issue.pr_title == "fix(verify): fail closed"
    assert issue.depends_on == (22,)
    assert issue.contract_error == ""
    assert issue.session_name == "GL-023"


@pytest.mark.parametrize(
    "dependency,expected",
    [
        ("None", ()),
        ("none", ()),
        ("n/a", ()),
        ("-", ()),
        ("#22", (22,)),
        ("#22, #24", (22, 24)),
        ("#22 and #24", (22, 24)),
        ("`#22`", (22,)),
        ("#22, #22", (22,)),
    ],
)
def test_dependency_forms_are_all_read(dependency, expected):
    assert rr.parse_issue(issue_json(30, dependency=dependency)).depends_on == expected


def test_an_issue_never_depends_on_itself():
    assert rr.parse_issue(issue_json(30, dependency="#30, #22")).depends_on == (22,)


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"branch": ""}, "no `- Branch:"),
        ({"branch": "random-branch"}, "not a conventional"),
        ({"branch": "../../etc/passwd"}, "not a conventional"),
        ({"pr_title": "did some stuff"}, "not a Conventional Commit"),
    ],
)
def test_a_malformed_delivery_block_is_recorded_not_guessed(kw, needle):
    issue = rr.parse_issue(issue_json(30, **kw))
    assert needle in issue.contract_error


def test_a_missing_pr_title_line_is_a_contract_error():
    raw = issue_json(30)
    raw["body"] = "## Delivery\n- Dependency: None\n- Branch: `fix/thing`\n"
    assert "no `- Draft PR title" in rr.parse_issue(raw).contract_error


def test_an_issue_with_no_delivery_block_is_never_runnable():
    raw = issue_json(30)
    raw["body"] = "## Intent\nSomething vague.\n"
    issue = rr.parse_issue(raw)
    assert issue.contract_error
    assert rr.readiness(issue, {30: issue})[0] == rr.MALFORMED


def test_labels_survive_both_gh_shapes():
    raw = issue_json(30)
    raw["labels"] = ["roadmap", {"name": "status:ready"}]
    assert rr.parse_issue(raw).labels == ("roadmap", "status:ready")


def test_a_title_without_a_gl_prefix_still_names_a_session():
    issue = rr.parse_issue(issue_json(30, title="Add a thing"))
    assert issue.session_name == "issue-30"


# -------------------------------------------------------------------- readiness


def _by_number(*raws):
    issues = [rr.parse_issue(r) for r in raws]
    return issues, {i.number: i for i in issues}


def test_readiness_is_derived_only_from_github():
    issues, index = _by_number(
        issue_json(22, state="CLOSED"),
        issue_json(23, dependency="#22"),
        issue_json(24, dependency="#23"),
    )
    assert [rr.readiness(i, index)[0] for i in issues] == [rr.DONE, rr.READY, rr.WAITING]
    assert rr.next_ready(issues).number == 23


def test_the_lowest_numbered_ready_issue_wins():
    issues, _ = _by_number(issue_json(31), issue_json(24), issue_json(28))
    assert rr.next_ready(issues).number == 24


def test_a_blocked_label_stops_the_runner_picking_it_up():
    issues, index = _by_number(issue_json(23, labels=("roadmap", "status:blocked")))
    assert rr.readiness(issues[0], index)[0] == rr.BLOCKED
    assert rr.next_ready(issues) is None


def test_an_unknown_dependency_is_unmet_not_ignored():
    issues, index = _by_number(issue_json(23, dependency="#999"))
    state, detail = rr.readiness(issues[0], index)
    assert state == rr.WAITING
    assert "#999" in detail and "not a roadmap issue" in detail


def test_a_closed_issue_is_done_even_if_its_body_is_malformed():
    issues, index = _by_number(issue_json(22, state="CLOSED", branch=""))
    assert rr.readiness(issues[0], index)[0] == rr.DONE


def test_nothing_is_ready_when_the_roadmap_is_finished():
    issues, _ = _by_number(issue_json(22, state="CLOSED"), issue_json(23, state="CLOSED"))
    assert rr.next_ready(issues) is None


# ----------------------------------------------------------------------- prompt


def test_the_fresh_prompt_carries_the_whole_contract():
    issue = rr.parse_issue(issue_json(23, dependency="#22"))
    prompt = rr.build_prompt(issue, repo="acme/proj", base="main", resuming=False)
    assert "https://github.com/acme/proj/issues/23" in prompt
    assert "`fix/thing`" in prompt
    assert "`fix(verify): fail closed`" in prompt
    assert "Closes #23" in prompt
    assert "origin/main" in prompt
    assert "Never merge or deploy." in prompt
    assert "comment with the exact blocker and stop" in prompt


def test_an_existing_branch_switches_to_the_recovery_prompt():
    issue = rr.parse_issue(issue_json(23))
    prompt = rr.build_prompt(issue, repo="acme/proj", base="main", resuming=True)
    assert "already exists" in prompt
    assert "reproduce the current blocker" in prompt
    assert "Never merge or deploy." in prompt
    assert "Closes #23" in prompt


def test_the_prompt_never_leaves_an_unsubstituted_placeholder():
    issue = rr.parse_issue(issue_json(23, dependency="#22"))
    for resuming in (False, True):
        prompt = rr.build_prompt(issue, repo="acme/proj", base="main", resuming=resuming)
        assert "{" not in prompt and "}" not in prompt


# ----------------------------------------------------------------- no-merge rule


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "merge", "41"],
        ["gh", "release", "create", "v1"],
        ["git", "push", "origin", "main"],
        ["git", "reset", "--hard", "origin/main"],
        ["git", "rebase", "origin/main"],
        ["git", "cherry-pick", "abc123"],
        ["git", "tag", "v1"],
        ["git", "merge", "feat/other"],
        ["/usr/bin/git", "push", "origin", "main"],
    ],
)
def test_the_runner_cannot_merge_publish_or_rewrite_history(argv):
    with pytest.raises(rr.RunnerError):
        rr._forbid(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "merge-base", "HEAD", "origin/main"],
        ["git", "fetch", "origin", "main"],
        ["git", "switch", "main"],
        ["git", "ls-remote", "--heads", "origin", "fix/thing"],
        ["gh", "pr", "list", "--head", "fix/thing"],
        ["gh", "issue", "comment", "23"],
    ],
)
def test_the_reads_and_fast_forwards_it_needs_are_allowed(argv):
    rr._forbid(argv)


def test_no_command_in_a_whole_successful_run_is_forbidden():
    """The guard is only useful if the happy path never trips it."""
    runner, _ = _passing_runner()
    assert orch(runner).cmd_run(23) == 0
    for call in runner.calls + runner.streamed:
        rr._forbid(call)  # raises if the runner ever reached for a merge


# ------------------------------------------------------------------- base branch


def _base_runner(over=None):
    script = {
        ("git", "status"): "",
        ("git", "switch"): "",
        ("git", "fetch"): "",
        ("git", "rev-parse", "HEAD"): "cafe1234cafe1234\n",
        ("git", "rev-parse", "origin/main"): "cafe1234cafe1234\n",
        ("git", "merge-base"): "cafe1234cafe1234\n",
    }
    script.update(over or {})
    return FakeRunner(script)


def test_uncommitted_tracked_changes_stop_the_run_before_anything_else():
    runner = _base_runner({("git", "status"): " M src/greenlight/cli.py\n"})
    with pytest.raises(rr.RunnerError, match="uncommitted tracked changes"):
        orch(runner).prepare_base()
    assert not runner.ran("git", "switch")


def test_untracked_files_do_not_stop_the_run():
    runner = _base_runner()
    assert orch(runner).prepare_base() == "cafe1234cafe1234"
    assert runner.ran("git", "status")[0][2:] == ["--porcelain", "-uno"]


def test_a_stale_base_is_fast_forwarded_never_merged():
    runner = _base_runner({
        ("git", "rev-parse", "HEAD"): "old0000\n",
        ("git", "merge-base"): "old0000\n",
    })
    orch(runner).prepare_base()
    assert runner.ran("git", "merge") == [["git", "merge", "--ff-only", "origin/main"]]


def test_a_diverged_base_is_a_human_problem():
    runner = _base_runner({
        ("git", "rev-parse", "HEAD"): "local999\n",
        ("git", "merge-base"): "ancestor1\n",
    })
    with pytest.raises(rr.RunnerError, match="not a fast-forward"):
        orch(runner).prepare_base()
    assert not runner.ran("git", "merge")


def test_an_existing_local_or_remote_branch_is_detected():
    fresh = FakeRunner({
        ("git", "rev-parse", "--verify"): rr.Result([], 1),
        ("git", "ls-remote"): "",
    })
    assert orch(fresh).branch_exists("fix/thing") is False

    local = FakeRunner({("git", "rev-parse", "--verify"): "abc\n"})
    assert orch(local).branch_exists("fix/thing") is True

    remote = FakeRunner({
        ("git", "rev-parse", "--verify"): rr.Result([], 1),
        ("git", "ls-remote"): "abc\trefs/heads/fix/thing\n",
    })
    assert orch(remote).branch_exists("fix/thing") is True


# ------------------------------------------------------- completion contract


def _pr(**over):
    pr = {
        "number": 42,
        "url": "https://github.com/acme/proj/pull/42",
        "title": "fix(verify): fail closed",
        "body": "Intent, validation, risks.\n\nCloses #23\n",
        "isDraft": True,
        "baseRefName": "main",
    }
    pr.update(over)
    return pr


def _passing_runner(*, prs=None, comments=None, over=None):
    script = {
        ("gh", "issue", "list"): [issue_json(22, state="CLOSED"), issue_json(23, dependency="#22")],
        ("gh", "pr", "list"): [_pr()] if prs is None else prs,
        ("gh", "issue", "view"): {
            "comments": [{"body": "Draft PR: https://github.com/acme/proj/pull/42"}]
            if comments is None else comments
        },
        ("gh", "issue", "comment"): "",
        ("gh", "issue", "edit"): "",
        ("git", "status"): "",
        ("git", "switch"): "",
        ("git", "fetch"): "",
        ("git", "rev-parse", "HEAD"): "cafe1234cafe1234\n",
        ("git", "rev-parse", "origin/main"): "cafe1234cafe1234\n",
        ("git", "rev-parse", "--verify"): rr.Result([], 1),
        ("git", "rev-parse", "--absolute-git-dir"): "/repo/.git\n",
        ("git", "merge-base"): "cafe1234cafe1234\n",
        ("git", "ls-remote"): "abc\trefs/heads/fix/thing\n",
    }
    script.update(over or {})
    runner = FakeRunner(script)
    return runner, orch(runner)


def test_a_complete_contract_passes_and_stops_for_a_human():
    runner, o = _passing_runner()
    assert o.cmd_run(23) == 0
    assert len(runner.streamed) == 1
    assert runner.streamed[0][:3] == ["pi", "-p", "--approve"]
    # It stops rather than continuing to the next issue.
    assert len(runner.streamed) == 1


@pytest.mark.parametrize(
    "prs,needle",
    [
        ([], "no open pull request"),
        ([_pr(isDraft=False)], "not a draft"),
        ([_pr(title="fix(verify): something else")], "the issue configured"),
        ([_pr(body="No linkage here.")], "Closes #23"),
        ([_pr(), _pr(number=43)], "expected one"),
        ([_pr(baseRefName="fix/other-task")], "targets 'fix/other-task'"),
        ([_pr(baseRefName="")], "not 'main'"),
    ],
)
def test_an_unmet_contract_blocks_the_issue_instead_of_passing(prs, needle):
    runner, o = _passing_runner(prs=prs)
    assert o.cmd_run(23) == 1
    comments = runner.ran("gh", "issue", "comment")
    assert comments and needle in comments[0][-1]
    edits = runner.ran("gh", "issue", "edit")
    assert edits and "status:blocked" in edits[0]


def test_an_unpushed_branch_is_an_unmet_contract():
    runner, o = _passing_runner(prs=[], over={("git", "ls-remote"): ""})
    assert o.cmd_run(23) == 1
    assert "never pushed" in runner.ran("gh", "issue", "comment")[0][-1]


def test_a_lowercase_fixes_reference_still_closes_the_issue():
    _, o = _passing_runner(prs=[_pr(body="fixes #23")])
    assert o.cmd_run(23) == 0


def test_a_session_that_exits_nonzero_but_left_a_valid_pr_still_passes():
    """Exit codes are noisy; the PR is the evidence."""
    runner, o = _passing_runner()
    runner.stream_code = 1
    assert o.cmd_run(23) == 0


def test_a_hung_session_is_reported_and_still_verified():
    runner, o = _passing_runner(prs=[])
    runner.stream_code = 124
    assert o.cmd_run(23) == 1
    assert "`124`" in runner.ran("gh", "issue", "comment")[0][-1]


def test_a_missing_issue_comment_is_backfilled_with_the_pr_link():
    runner, o = _passing_runner(comments=[])
    assert o.cmd_run(23) == 0
    posted = runner.ran("gh", "issue", "comment")
    assert posted and "https://github.com/acme/proj/pull/42" in posted[0][-1]


def test_an_existing_issue_comment_is_not_duplicated():
    runner, o = _passing_runner()
    assert o.cmd_run(23) == 0
    assert not runner.ran("gh", "issue", "comment")


def test_a_pr_linked_only_by_number_counts_as_a_comment():
    runner, o = _passing_runner(comments=[{"body": "opened #42 for this"}])
    assert o.cmd_run(23) == 0
    assert not runner.ran("gh", "issue", "comment")


def test_failing_to_record_a_blocker_does_not_crash_the_runner():
    runner, o = _passing_runner(prs=[], over={("gh", "issue", "comment"): rr.Result([], 1)})
    assert o.cmd_run(23) == 1


# ------------------------------------------------------------------- run guards


def test_an_unready_issue_is_refused_with_the_reason():
    _, o = _passing_runner(over={
        ("gh", "issue", "list"): [issue_json(22), issue_json(23, dependency="#22")],
    })
    with pytest.raises(rr.RunnerError, match="waiting on #22"):
        o.cmd_run(23)


def test_an_unknown_issue_is_refused():
    _, o = _passing_runner()
    with pytest.raises(rr.RunnerError, match="#77 is not"):
        o.cmd_run(77)


def test_no_ready_issue_exits_three_without_starting_a_session():
    runner, o = _passing_runner(over={
        ("gh", "issue", "list"): [
            issue_json(22, state="CLOSED"),
            issue_json(23, state="CLOSED"),
        ],
    })
    assert o.cmd_run() == 3
    assert not runner.streamed


def test_run_without_an_argument_takes_the_next_ready_issue():
    runner, o = _passing_runner()
    assert o.cmd_run() == 0
    assert "https://github.com/acme/proj/issues/23" in runner.streamed[0][-1]


def test_a_dry_run_touches_nothing():
    runner, o = _passing_runner()
    assert o.cmd_run(23, dry_run=True) == 0
    assert not runner.streamed
    assert not runner.ran("git", "switch")
    assert not runner.ran("git", "fetch")
    assert not runner.ran("gh", "issue", "comment")
    assert not runner.ran("gh", "issue", "edit")


def test_an_empty_roadmap_is_an_error_not_an_empty_success():
    _, o = _passing_runner(over={("gh", "issue", "list"): []})
    with pytest.raises(rr.RunnerError, match="no issues labelled"):
        o.cmd_run()


def test_a_truncated_issue_list_is_refused_rather_than_half_trusted():
    """At the page limit a real dependency would look like an unknown one."""
    many = [issue_json(n, state="CLOSED") for n in range(1, 201)]
    _, o = _passing_runner(over={("gh", "issue", "list"): many})
    with pytest.raises(rr.RunnerError, match="raise the limit"):
        o.cmd_run()


def test_unreadable_gh_json_fails_closed():
    _, o = _passing_runner(over={("gh", "issue", "list"): "not json at all"})
    with pytest.raises(rr.RunnerError, match="unreadable JSON"):
        o.cmd_run()


def test_the_session_log_lands_outside_the_worktree():
    runner, o = _passing_runner()
    o.cmd_run(23)
    log = runner.logs[0]
    assert log is not None
    assert log.parent == Path("/repo/.git/roadmap-runs")
    assert log.name.startswith("23-")


# ----------------------------------------------------------------- label upkeep


def test_sync_labels_marks_exactly_the_ready_issues():
    runner = FakeRunner({
        ("gh", "issue", "list"): [
            issue_json(22, state="CLOSED"),
            issue_json(23, dependency="#22"),
            issue_json(24, dependency="#23", labels=("roadmap", "status:ready")),
        ],
        ("gh", "issue", "edit"): "",
    })
    assert orch(runner).cmd_sync_labels() == 0
    edits = runner.ran("gh", "issue", "edit")
    assert [(e[3], e[-2], e[-1]) for e in edits] == [
        ("23", "--add-label", "status:ready"),
        ("24", "--remove-label", "status:ready"),
    ]


def test_sync_labels_leaves_closed_issues_alone():
    runner = FakeRunner({
        ("gh", "issue", "list"): [issue_json(22, state="CLOSED", labels=("roadmap",
                                                                        "status:ready"))],
        ("gh", "issue", "edit"): "",
    })
    orch(runner).cmd_sync_labels()
    assert not runner.ran("gh", "issue", "edit")


def test_sync_labels_dry_run_changes_nothing():
    runner = FakeRunner({
        ("gh", "issue", "list"): [issue_json(23)],
        ("gh", "issue", "edit"): "",
    })
    orch(runner).cmd_sync_labels(dry_run=True)
    assert not runner.ran("gh", "issue", "edit")


def test_a_run_can_opt_out_of_label_upkeep():
    runner, o = _passing_runner()
    assert o.cmd_run(23, sync=False) == 0
    assert not runner.ran("gh", "issue", "edit")


# ------------------------------------------------------------------- reporting


def test_status_reports_every_issue_and_the_next_one(capsys):
    runner = FakeRunner({
        ("gh", "issue", "list"): [
            issue_json(22, state="CLOSED"),
            issue_json(23, dependency="#22"),
            issue_json(24, dependency="#23"),
            issue_json(25, labels=("roadmap", "status:blocked")),
        ],
    })
    assert orch(runner).cmd_status() == 0
    out = capsys.readouterr()
    assert "#22" in out.out and "done" in out.out
    assert "ready" in out.out and "waiting" in out.out and "blocked" in out.out
    assert "next: #23" in out.err
    assert not runner.ran("gh", "issue", "edit")  # status never mutates


def test_next_prints_the_url_and_exits_three_when_nothing_is_ready(capsys):
    runner = FakeRunner({("gh", "issue", "list"): [issue_json(23, dependency="#22")]})
    assert orch(runner).cmd_next() == 3
    runner = FakeRunner({("gh", "issue", "list"): [issue_json(23)]})
    assert orch(runner).cmd_next() == 0
    assert "https://github.com/acme/proj/issues/23" in capsys.readouterr().out


# -------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "raw,gone",
    [
        ("token=ghp_abcdefghijklmnopqrstuvwxyz01", "ghp_abcdefghijklmnopqrstuvwxyz01"),
        ("Authorization: sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
        ("api_key: hunter2secretvalue", "hunter2secretvalue"),
        ("PASSWORD=correcthorsebattery", "correcthorsebattery"),
    ],
)
def test_secrets_never_reach_a_log_or_a_github_comment(raw, gone):
    assert gone not in rr.redact(raw)


def test_redaction_leaves_ordinary_text_intact():
    text = "branch fix/thing pushed; PR #42 opened"
    assert rr.redact(text) == text


def test_a_blocker_comment_is_redacted():
    runner, o = _passing_runner(prs=[])
    o.cmd_run(23)
    assert "ghp_" not in runner.ran("gh", "issue", "comment")[0][-1]


# --------------------------------------------------------------------- plumbing


def test_every_gh_call_is_pinned_to_the_configured_repo():
    runner, o = _passing_runner()
    o.cmd_run(23)
    for call in runner.ran("gh"):
        assert "--repo" in call and call[call.index("--repo") + 1] == "acme/proj"


def test_a_silent_hang_still_hits_the_ceiling(tmp_path):
    """The real Runner, a real child. A hung session is usually a silent one, so
    the deadline must not depend on output arriving."""
    log = tmp_path / "hang.log"
    code = rr.Runner().stream(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=1,
        log_path=log,
    )
    assert code == 124


def test_the_session_log_captures_output_and_the_exit_code(tmp_path, capsys):
    log = tmp_path / "run.log"
    code = rr.Runner().stream(
        [sys.executable, "-c", "print('hello from the session'); raise SystemExit(3)"],
        cwd=tmp_path,
        timeout=30,
        log_path=log,
    )
    assert code == 3
    assert "hello from the session" in log.read_text()
    assert "hello from the session" in capsys.readouterr().out


def test_a_session_log_is_redacted(tmp_path):
    log = tmp_path / "secret.log"
    rr.Runner().stream(
        [sys.executable, "-c", "print('token=ghp_abcdefghijklmnopqrstuvwxyz01')"],
        cwd=tmp_path,
        timeout=30,
        log_path=log,
    )
    assert "ghp_abcdefghijklmnopqrstuvwxyz01" not in log.read_text()


def test_a_missing_tool_is_a_clean_error_not_a_traceback(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("pi")
    monkeypatch.setattr(rr.subprocess, "run", boom)
    with pytest.raises(rr.RunnerError, match="not installed or not on PATH"):
        rr.Runner().run(["pi", "--version"])


def test_cli_wiring_reaches_each_command(monkeypatch):
    seen = {}
    for command, argv in (
        ("status", ["status"]),
        ("next", ["next"]),
        ("sync-labels", ["sync-labels"]),
        ("run", ["run", "23", "--dry-run"]),
    ):
        monkeypatch.setattr(rr.Orchestrator, "cmd_status", lambda self: seen.setdefault("status", 0))
        monkeypatch.setattr(rr.Orchestrator, "cmd_next", lambda self: seen.setdefault("next", 0))
        monkeypatch.setattr(rr.Orchestrator, "cmd_sync_labels",
                            lambda self, **k: seen.setdefault("sync-labels", 0))
        monkeypatch.setattr(rr.Orchestrator, "cmd_run",
                            lambda self, n=None, **k: seen.setdefault("run", 0))
        assert rr.main(argv) == 0
        assert command in seen


def test_a_runner_error_becomes_exit_two(monkeypatch):
    monkeypatch.setattr(
        rr.Orchestrator, "cmd_status",
        lambda self: (_ for _ in ()).throw(rr.RunnerError("nope")),
    )
    assert rr.main(["status"]) == 2


def test_zero_timeout_disables_the_ceiling(monkeypatch):
    captured = {}
    monkeypatch.setattr(rr.Orchestrator, "cmd_run",
                        lambda self, n=None, **k: captured.update(k) or 0)
    rr.main(["run", "--timeout", "0"])
    assert captured["timeout"] is None
    rr.main(["run", "--timeout", "60"])
    assert captured["timeout"] == 60
