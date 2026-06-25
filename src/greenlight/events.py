"""Structured pipeline events — the machine-readable handoff contract.

greenlight's human-facing progress is the `util.step/info/ok` stderr lines. This
module emits a *parallel* structured stream so a UI (e.g. the pi extension that
renders the live pipeline card) can show stage-by-stage progress without parsing
prose.

It is strictly additive and off by default: events are written as JSONL to the
path in the `GREENLIGHT_EVENTS` env var (one JSON object per line). When that var
is unset, every emit is a no-op, so the gate behaves exactly as before. The
orchestrator owns all control flow; this only observes it.

The CLI `run`/`hook` paths call `enable_default()` to point the sink at a
deterministic per-repo file (`default_path`) so `greenlight watch` can tail the
same stream the gate writes — without the two processes sharing any env. A
caller-set `GREENLIGHT_EVENTS` (e.g. the pi extension's temp file) always wins.

Event shape: {"ts": <float epoch>, "type": <str>, ...payload}. Types map 1:1 to
the real pipeline stages:

  run_start     {branch, classification, files}
  intent        {source: "supplied"|"reconstructed", text}
  lint          {status: "pass"|"fail"|"skip", fixed: bool}
  review_round  {round, max_rounds}
  reviewer      {name, round, findings, blocking, items}
                items: [{severity, file, line, description, blocks}] on the
                completion event (omitted on the "started" event where findings
                is null). Consumers may ignore it; it is additive.
  fix           {round, findings}
  verify        {target: "backend"|"frontend", status, evidence}
  pr            {status: "open"|"exists"|"skip"|"fail", url}
  run_end       {passed}
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_sink = None  # lazily opened append handle, or False once we've given up


def default_path(repo_root: str) -> Path:
    """Per-repo events file under the greenlight state dir.

    Deterministic from the repo root so `greenlight watch` can resolve the same
    file the gate writes to, without the two processes sharing any env.
    """
    from .util import repo_id, state_dir

    return state_dir() / "runs" / repo_id(repo_root) / "events.jsonl"


def enable_default(repo_root: str, *, truncate: bool = True) -> str:
    """Point the sink at the per-repo default path unless one is already set.

    Returns the active events path. Call this before the pipeline runs (and
    before any emit, so the lazy sink opens against the right file). When the
    caller already set GREENLIGHT_EVENTS (e.g. the pi extension's temp file), it
    wins and the default is left alone.
    """
    existing = os.environ.get("GREENLIGHT_EVENTS")
    if existing:
        return existing
    p = default_path(repo_root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if truncate:
            p.write_text("")
    except OSError:
        return ""
    os.environ["GREENLIGHT_EVENTS"] = str(p)
    return str(p)


def _handle():
    """Return the open events sink, or None when disabled/unavailable."""
    global _sink
    if _sink is False:
        return None
    if _sink is not None:
        return _sink
    path = os.environ.get("GREENLIGHT_EVENTS")
    if not path:
        _sink = False
        return None
    try:
        # Line-buffered append so a UI tailing the file sees events promptly and
        # concurrent runs (separate branches) don't clobber each other.
        _sink = open(path, "a", buffering=1, encoding="utf-8")
    except OSError as e:
        print(f"   (events disabled: {e})", file=sys.stderr)
        _sink = False
        return None
    # This process now owns the stream via the open handle, so drop the env var:
    # otherwise child subprocesses (lint/verify, agent `pi` calls, and any nested
    # `greenlight run` they spawn) would inherit it and append their own events
    # into our file, corrupting the stream a UI is reading.
    os.environ.pop("GREENLIGHT_EVENTS", None)
    return _sink


def emit(type: str, **payload: Any) -> None:
    """Append one event. No-op unless GREENLIGHT_EVENTS is set."""
    fh = _handle()
    if fh is None:
        return
    rec = {"ts": time.time(), "type": type, **payload}
    try:
        fh.write(json.dumps(rec, default=str) + "\n")
    except (OSError, ValueError):
        # Never let telemetry break the pipeline.
        pass
