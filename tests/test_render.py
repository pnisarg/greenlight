"""The Python reducer + card renderer used by `greenlight watch`.

Mirrors the node reducer tests so both twins stay in sync on the event contract.
"""
import json

from greenlight import render

PASSING = "\n".join(
    json.dumps(e)
    for e in [
        {"ts": 1, "type": "run_start", "branch": "feat/demo", "classification": "backend",
         "files": [".greenlight.toml", "calc.py"]},
        {"ts": 2, "type": "intent", "source": "supplied", "text": "Add a mul() helper"},
        {"ts": 3, "type": "lint", "status": "skip", "fixed": False},
        {"ts": 4, "type": "review_round", "round": 1, "max_rounds": 3},
        {"ts": 5, "type": "reviewer", "name": "brutal", "round": 1, "findings": None, "blocking": None},
        {"ts": 6, "type": "reviewer", "name": "brutal", "round": 1, "findings": 0, "blocking": 0},
        {"ts": 7, "type": "reviewer", "name": "security", "round": 1, "findings": None, "blocking": None},
        {"ts": 8, "type": "reviewer", "name": "security", "round": 1, "findings": 0, "blocking": 0},
        {"ts": 9, "type": "verify", "target": "backend", "status": "skip", "evidence": []},
        {"ts": 10, "type": "pr", "status": "open", "url": "https://x/y/pull/1"},
        {"ts": 11, "type": "run_end", "passed": True},
    ]
)


def test_reduces_full_passing_run():
    s = render.state_from(PASSING)
    assert s.passed is True
    assert s.branch == "feat/demo"
    assert s.classification == "backend"
    assert s.file_count == 2
    assert s.intent_source == "supplied"
    assert s.stages["intent"] == "done"
    assert s.stages["lint"] == "skip"
    assert s.stages["review"] == "done"
    assert s.stages["verify"] == "skip"
    assert s.stages["pr"] == "done"
    assert len(s.reviewers) == 2
    assert all(r.status == "done" for r in s.reviewers)
    assert s.parse_errors == 0


def test_two_phase_reviewer_flips_running_to_done():
    s = render.State()
    render.apply_lines(s, "\n".join(json.dumps(e) for e in [
        {"type": "review_round", "round": 1, "max_rounds": 3},
        {"type": "reviewer", "name": "brutal", "round": 1, "findings": None, "blocking": None},
    ]))
    assert s.reviewers[0].status == "running"
    assert s.stages["review"] == "running"
    render.reduce(s, {"type": "reviewer", "name": "brutal", "round": 1, "findings": 3, "blocking": 1})
    assert s.reviewers[0].status == "done"
    assert s.reviewers[0].findings == 3
    assert s.reviewers[0].blocking == 1


def test_fix_bumps_counter_and_reruns():
    s = render.State()
    render.apply_lines(s, "\n".join(json.dumps(e) for e in [
        {"type": "review_round", "round": 1, "max_rounds": 3},
        {"type": "reviewer", "name": "brutal", "round": 1, "findings": 2, "blocking": 1},
        {"type": "fix", "round": 1, "findings": 1},
    ]))
    assert s.fixes == 1
    assert s.reviewers[0].status == "running"


def test_failure_marks_blocking_gate_and_leaves_rest_pending():
    s = render.state_from("\n".join(json.dumps(e) for e in [
        {"type": "run_start", "branch": "feat/x", "classification": "backend", "files": ["a.py"]},
        {"type": "intent", "source": "supplied", "text": "x"},
        {"type": "lint", "status": "pass", "fixed": False},
        {"type": "review_round", "round": 3, "max_rounds": 3},
        {"type": "reviewer", "name": "brutal", "round": 3, "findings": 1, "blocking": 1},
        {"type": "run_end", "passed": False},
    ]))
    assert s.passed is False
    assert s.stages["lint"] == "done"
    assert s.stages["review"] == "fail"
    assert s.stages["verify"] == "pending"
    assert s.stages["pr"] == "pending"


def test_malformed_lines_counted_not_raised():
    s = render.state_from('not json\n{"type":"run_end","passed":true}\n\n{bad')
    assert s.parse_errors == 2
    assert s.passed is True


def test_render_card_has_handoff_bookends():
    lines = render.render_card(render.state_from(PASSING), color=False)
    assert lines[0].startswith("greenlight ")
    assert any("intent" in ln and "supplied by agent" in ln for ln in lines)
    assert any("brutal" in ln and "blocking" in ln for ln in lines)
    assert "PASSED" in lines[-1]


def test_render_card_color_wraps_ansi():
    lines = render.render_card(render.state_from(PASSING), color=True)
    assert any("\033[" in ln for ln in lines)
    plain = render.render_card(render.state_from(PASSING), color=False)
    assert all("\033[" not in ln for ln in plain)
