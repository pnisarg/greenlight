"""Structured pipeline events — the machine-readable handoff contract.

greenlight's human-facing progress is the `util.step/info/ok` stderr lines. This
module emits a *parallel* structured stream so a UI (e.g. the pi extension that
renders the live pipeline card) can show stage-by-stage progress without parsing
prose.

It is strictly additive and off by default: events are written as JSONL to the
path in the `GREENLIGHT_EVENTS` env var (one JSON object per line). When that var
is unset, every emit is a no-op, so the gate behaves exactly as before. The
orchestrator owns all control flow; this only observes it.

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
from typing import Any

_sink = None  # lazily opened append handle, or False once we've given up


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
