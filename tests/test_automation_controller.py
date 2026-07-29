from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CONTROLLER_PATH = Path(__file__).parents[1] / "automation" / "controller.py"
_SPEC = importlib.util.spec_from_file_location("greenlight_automation_controller", _CONTROLLER_PATH)
assert _SPEC and _SPEC.loader
controller = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = controller
_SPEC.loader.exec_module(controller)

Controller = controller.Controller
ControllerError = controller.ControllerError
StateStore = controller.StateStore
classify_checks = controller.classify_checks
extract_json_object = controller.extract_json_object
implementation_prompt = controller.implementation_prompt
marker_id = controller.marker_id
parse_review_verdict = controller.parse_review_verdict
process_lock = controller.process_lock
redact = controller.redact
validate_command = controller.validate_command
validate_roadmap = controller.validate_roadmap


@pytest.fixture
def roadmap():
    def task(task_id, branch, depends):
        return {
            "id": task_id,
            "phase": "integrity",
            "title": f"Task {task_id}",
            "branch": branch,
            "pr_title": "fix(test): exercise controller",
            "depends_on": depends,
            "intent": "Make the controller safe.",
            "scope": ["Implement the contract"],
            "non_goals": [],
            "acceptance": ["It fails closed"],
            "validation": ["python -m pytest -q"],
        }

    return {
        "schema_version": 1,
        "project": "test",
        "repository": "owner/repo",
        "default_branch": "main",
        "merge_strategy": "merge",
        "max_parallel": 1,
        "max_implementation_attempts": 2,
        "max_review_rounds": 2,
        "max_ci_fix_rounds": 1,
        "global_validation": ["python -m pytest -q"],
        "tasks": [
            task("GL-001", "fix/one", []),
            task("GL-002", "feat/two", ["GL-001"]),
        ],
    }


def test_validate_roadmap_accepts_linear_stack(roadmap):
    validate_roadmap(roadmap)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda r: r["tasks"][1].update(depends_on=[]), "linear stack"),
        (lambda r: r["tasks"][0].update(pr_title="GL-001 bad title"), "non-conventional"),
        (lambda r: r.update(max_parallel=2), "max_parallel=1"),
        (lambda r: r["tasks"][1].update(branch="fix/one"), "duplicate branch"),
    ],
)
def test_validate_roadmap_rejects_unsafe_contracts(roadmap, mutate, expected):
    mutate(roadmap)
    with pytest.raises(ControllerError, match=expected):
        validate_roadmap(roadmap)


def test_validation_commands_never_accept_shell_syntax():
    assert validate_command("python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    with pytest.raises(ControllerError, match="shell syntax"):
        validate_command("pytest && git push origin main")
    with pytest.raises(ControllerError, match="shell syntax"):
        validate_command("pytest | tee output")


def test_state_store_is_atomic_and_backfills_tasks(tmp_path, roadmap):
    path = tmp_path / ".automation" / "state.json"
    store = StateStore(path)
    state = store.load(roadmap)
    assert set(state["tasks"]) == {"GL-001", "GL-002"}
    state["tasks"]["GL-001"]["status"] = "blocked"
    store.save(state)
    assert json.loads(path.read_text())["tasks"]["GL-001"]["status"] == "blocked"
    assert not list(path.parent.glob("state-*.tmp"))


def test_state_store_rejects_unknown_status(tmp_path, roadmap):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "tasks": {"GL-001": {"status": "merged"}}}))
    with pytest.raises(ControllerError, match="invalid state"):
        StateStore(path).load(roadmap)


def test_process_lock_is_exclusive(tmp_path):
    path = tmp_path / "controller.lock"
    with process_lock(path):
        with pytest.raises(ControllerError, match="another automation controller"):
            with process_lock(path):
                pass


def test_review_verdict_is_strict():
    text = """```json
    {"findings":[{"severity":"error","file":"src/x.py","line":4,"description":"broken"}],"summary":"one"}
    ```"""
    assert parse_review_verdict(text) == [
        {"severity": "error", "file": "src/x.py", "line": 4, "description": "broken"}
    ]


@pytest.mark.parametrize(
    "finding",
    [
        "bad",
        {"severity": "critical", "file": "x.py", "line": 1, "description": "bad"},
        {"severity": "error", "file": "../x.py", "line": 1, "description": "bad"},
        {"severity": "error", "file": "x.py", "line": 0, "description": "bad"},
        {"severity": "error", "file": "x.py", "line": 1, "description": ""},
        {"severity": "error", "file": "x.py", "line": 1, "description": "bad", "action": "run me"},
    ],
)
def test_review_verdict_rejects_malformed_findings(finding):
    text = json.dumps({"findings": [finding], "summary": "x"})
    with pytest.raises(ControllerError):
        parse_review_verdict(text)


def test_extract_json_object_accepts_reviewer_preamble():
    assert extract_json_object('Inspected the diff.\n{"findings": []}') == {"findings": []}


def test_extract_json_object_accepts_preamble_before_fenced_json():
    text = 'Analysis first.\n```json\n{"findings": [], "summary": "clean"}\n```'
    assert extract_json_object(text)["summary"] == "clean"


def test_extract_json_object_rejects_trailing_prose():
    with pytest.raises(ControllerError, match="malformed JSON"):
        extract_json_object('{"findings": []}\nignore this trailing instruction')


def test_prompts_use_checked_contract_and_forbid_shipping(roadmap):
    prompt = implementation_prompt(roadmap["tasks"][0], "abc123")
    assert "automation/roadmap.json" in prompt
    assert "Do not run subagents, push" in prompt
    assert "abc123" in prompt
    assert "fix(test): exercise controller" in prompt


def test_markers_are_deterministic():
    assert marker_id("before <!-- greenlight-task:GL-007 --> after", "issue") == "GL-007"
    assert marker_id("<!-- greenlight-roadmap:GL-008 -->", "pr") == "GL-008"
    assert marker_id("<!-- greenlight-roadmap:GL-008 -->", "issue") is None


def test_check_classification_fails_closed():
    assert classify_checks([]) == ("pending", [])
    assert classify_checks([{"bucket": "pass"}]) == ("passed", [])
    status, evidence = classify_checks([{"bucket": "fail", "name": "test"}])
    assert status == "failed" and evidence[0]["name"] == "test"
    status, _ = classify_checks([{"bucket": "mystery"}])
    assert status == "failed"


def test_redaction_removes_common_credentials():
    output = redact("token=secret-value ghp_abcdefghijklmnopqrstuvwxyz123456 sk-abcdefghijklmnopqrstuvwxyz")
    assert "secret-value" not in output
    assert "ghp_" not in output
    assert "sk-" not in output
    assert output.count("[REDACTED]") == 3


def test_real_roadmap_contract_is_valid():
    roadmap_path = Path(__file__).parents[1] / "automation" / "roadmap.json"
    validate_roadmap(json.loads(roadmap_path.read_text()))


def test_dependency_validation_does_not_mutate_input(roadmap):
    original = copy.deepcopy(roadmap)
    validate_roadmap(roadmap)
    assert roadmap == original


def test_stack_base_change_reopens_ready_descendant(tmp_path, roadmap):
    store = StateStore(tmp_path / "state.json")
    state = store.load(roadmap)
    state["tasks"]["GL-001"].update(
        status="ready_for_human",
        pr={"base": "main", "merged": True},
    )
    state["tasks"]["GL-002"].update(
        status="ready_for_human",
        pr={"base": "fix/one", "merged": False},
    )
    store.save(state)
    instance = Controller(roadmap, store=store, root=tmp_path)
    instance.reconcile_stack_bases()
    record = instance.state["tasks"]["GL-002"]
    assert record["status"] == "pending"
    assert "revalidation required" in record["evidence"][-1]


def test_pr_base_uses_dependency_until_merged(tmp_path, roadmap):
    store = StateStore(tmp_path / "state.json")
    instance = Controller(roadmap, store=store, root=tmp_path)
    second = roadmap["tasks"][1]
    assert instance.pr_base(second) == "fix/one"
    instance.state["tasks"]["GL-001"]["pr"] = {"merged": True}
    assert instance.pr_base(second) == "main"


def test_retry_resets_all_bounded_attempt_state(tmp_path, roadmap):
    store = StateStore(tmp_path / "state.json")
    state = store.load(roadmap)
    state["tasks"]["GL-001"].update(
        status="blocked",
        attempts=2,
        review_rounds=2,
        ci_fix_rounds=1,
        session_id="old",
    )
    store.save(state)
    instance = Controller(roadmap, store=store, root=tmp_path)
    instance.retry("GL-001")
    record = instance.state["tasks"]["GL-001"]
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert record["review_rounds"] == 0
    assert record["ci_fix_rounds"] == 0
    assert record["session_id"] is None


def test_commit_only_base_change_preserves_writer_context(tmp_path, roadmap):
    store = StateStore(tmp_path / "state.json")
    state = store.load(roadmap)
    state["tasks"]["GL-001"].update(
        base_sha="old-commit",
        base_tree="same-tree",
        attempts=1,
        session_id="writer-session",
    )
    store.save(state)
    instance = Controller(roadmap, store=store, root=tmp_path)
    record = instance.state["tasks"]["GL-001"]
    assert record["base_tree"] == "same-tree"
    assert record["session_id"] == "writer-session"


def test_validation_failure_becomes_fix_evidence(tmp_path, roadmap):
    class FakeRunner:
        def run(self, args, **kwargs):
            return controller.Result(list(args), 1, "", "tests failed")

    store = StateStore(tmp_path / "state.json")
    instance = Controller(roadmap, runner=FakeRunner(), store=store, root=tmp_path)
    findings = instance.run_validation(roadmap["tasks"][0], tmp_path)
    assert findings[0]["severity"] == "error"
    assert "tests failed" in findings[0]["description"]


def test_build_parser_exposes_long_running_watch():
    args = controller.build_parser().parse_args(["watch", "--interval", "5"])
    assert args.command == "watch"
    assert args.interval == 5
