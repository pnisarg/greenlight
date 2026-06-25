"""Pure reducer + card renderer over the pipeline event stream.

This is the Python twin of the pi extension's `pipeline-state.ts`: it folds the
JSONL events (see `events.py`) into a pipeline snapshot and renders the same
stage-by-stage card. `greenlight watch` uses it to visualize the human
`git push greenlight` path, where the gate runs server-side and the user can't
inject a UI of their own.

Kept free of any I/O so it can be unit-tested with plain dicts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

STAGE_ORDER = ("intent", "lint", "review", "verify", "pr")
_STAGE_LABEL = {
    "intent": "intent",
    "lint": "lint",
    "review": "review",
    "verify": "verify",
    "pr": "PR",
}
_GLYPH = {"pending": "·", "running": "⟳", "done": "✓", "fail": "✗", "skip": "⊘"}


def _status_for(s: str) -> str:
    return {"pass": "done", "fail": "fail", "skip": "skip"}.get(s, "pending")


@dataclass
class Reviewer:
    name: str
    status: str = "running"
    findings: int | None = None
    blocking: int | None = None


@dataclass
class State:
    branch: str = ""
    classification: str = ""
    file_count: int = 0
    intent_source: str | None = None
    intent_text: str = ""
    stages: dict[str, str] = field(
        default_factory=lambda: {s: "pending" for s in STAGE_ORDER}
    )
    round: int = 0
    max_rounds: int = 0
    fixes: int = 0
    reviewers: list[Reviewer] = field(default_factory=list)
    verify: list[tuple[str, str]] = field(default_factory=list)  # (target, status)
    pr_status: str = ""
    pr_url: str = ""
    passed: bool | None = None
    parse_errors: int = 0


def _reviewer(state: State, name: str) -> Reviewer:
    for r in state.reviewers:
        if r.name == name:
            return r
    r = Reviewer(name=name)
    state.reviewers.append(r)
    return r


def _settle_review(state: State) -> None:
    if state.stages["review"] == "running":
        state.stages["review"] = "done"


def _aggregate_verify(state: State) -> str:
    statuses = [s for _, s in state.verify]
    if any(s == "fail" for s in statuses):
        return "fail"
    if statuses and all(s == "skip" for s in statuses):
        return "skip"
    if statuses:
        return "done"
    return "running"


def _finalize(state: State) -> None:
    if state.passed:
        for s in ("intent", "lint", "review", "verify"):
            if state.stages[s] in ("running", "pending"):
                state.stages[s] = "done"
        return
    for s in STAGE_ORDER:
        if state.stages[s] == "running":
            state.stages[s] = "fail"
            break


def reduce(state: State, ev: dict) -> State:
    """Apply one event. Order-tolerant."""
    t = ev.get("type")
    if t == "run_start":
        state.branch = str(ev.get("branch", ""))
        state.classification = str(ev.get("classification", ""))
        files = ev.get("files")
        state.file_count = len(files) if isinstance(files, list) else 0
        state.stages["intent"] = "running"
    elif t == "intent":
        state.intent_source = "supplied" if ev.get("source") == "supplied" else "reconstructed"
        state.intent_text = str(ev.get("text", ""))
        state.stages["intent"] = "done"
    elif t == "lint":
        state.stages["lint"] = _status_for(str(ev.get("status")))
    elif t == "review_round":
        state.stages["review"] = "running"
        state.round = int(ev.get("round", 0))
        state.max_rounds = int(ev.get("max_rounds", 0))
    elif t == "reviewer":
        r = _reviewer(state, str(ev.get("name", "")))
        findings = ev.get("findings")
        if findings is None:
            r.status = "running"
        else:
            r.status = "done"
            r.findings = int(findings)
            r.blocking = int(ev.get("blocking") or 0)
    elif t == "fix":
        state.fixes += 1
        for r in state.reviewers:
            r.status = "running"
    elif t == "verify":
        _settle_review(state)
        target = str(ev.get("target", ""))
        status = _status_for(str(ev.get("status")))
        for i, (tg, _) in enumerate(state.verify):
            if tg == target:
                state.verify[i] = (target, status)
                break
        else:
            state.verify.append((target, status))
        state.stages["verify"] = _aggregate_verify(state)
    elif t == "pr":
        _settle_review(state)
        state.pr_status = str(ev.get("status", ""))
        state.pr_url = str(ev.get("url", ""))
        state.stages["pr"] = {
            "open": "done",
            "exists": "done",
            "skip": "skip",
            "fail": "fail",
        }.get(state.pr_status, "pending")
    elif t == "run_end":
        state.passed = bool(ev.get("passed"))
        _finalize(state)
    return state


def apply_lines(state: State, text: str) -> int:
    applied = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reduce(state, json.loads(line))
            applied += 1
        except (json.JSONDecodeError, ValueError, TypeError):
            state.parse_errors += 1
    return applied


def state_from(text: str) -> State:
    s = State()
    apply_lines(s, text)
    return s


# --- rendering -------------------------------------------------------------

_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MUTED = "\033[90m"
_RESET = "\033[0m"

_STATUS_COLOR = {
    "pending": _DIM,
    "running": _CYAN,
    "done": _GREEN,
    "fail": _RED,
    "skip": _DIM,
}


def _c(s: str, code: str, color: bool) -> str:
    return f"{code}{s}{_RESET}" if color else s


def _stage_detail(state: State, stage: str) -> str:
    if stage == "intent":
        return f"{state.intent_source} by agent" if state.intent_source else ""
    if stage == "review":
        if state.max_rounds:
            fixes = f", {state.fixes} fix{'es' if state.fixes != 1 else ''}" if state.fixes else ""
            return f"round {state.round}/{state.max_rounds}{fixes}"
        return ""
    if stage == "verify":
        return ", ".join(f"{tg} {st}" for tg, st in state.verify)
    if stage == "pr":
        return state.pr_url if state.pr_url and state.pr_status != "fail" else state.pr_status
    return ""


def render_card(state: State, color: bool = True) -> list[str]:
    rows: list[str] = []
    if state.branch:
        n = state.file_count
        header = f"{state.branch}  ·  {state.classification or '?'}  ·  {n} file{'' if n == 1 else 's'}"
    else:
        header = "waiting for a run…"
    rows.append(_c("greenlight ", _BOLD, color) + _c(header, _MUTED, color))

    for stage in STAGE_ORDER:
        st = state.stages[stage]
        glyph = _c(_GLYPH[st], _STATUS_COLOR[st], color)
        label = _STAGE_LABEL[stage].ljust(7)
        line = f"  {glyph} {label}"
        detail = _stage_detail(state, stage)
        if detail:
            line += " " + _c(detail, _DIM, color)
        rows.append(line)
        if stage == "review" and state.reviewers:
            for r in state.reviewers:
                g = _c(_GLYPH[r.status], _STATUS_COLOR[r.status], color)
                sub = f"      {g} {r.name.ljust(10)}"
                if r.status == "done" and r.findings is not None:
                    plural = "" if r.findings == 1 else "s"
                    sub += " " + _c(f"{r.findings} finding{plural}, {r.blocking} blocking", _DIM, color)
                elif r.status == "running":
                    sub += " " + _c("running…", _DIM, color)
                rows.append(sub)

    if state.passed is not None:
        if state.passed:
            rows.append(_c("● PASSED — handed back to agent", _GREEN, color))
        else:
            rows.append(_c("● FAILED — nothing forwarded", _RED, color))
    return rows
