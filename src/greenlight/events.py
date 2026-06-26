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
caller-set `GREENLIGHT_EVENTS` (e.g. the pi extension's temp file) still owns the
primary stream, but `enable_default()` then *also* mirrors every event to the
per-repo path so `greenlight watch` can render extension-driven runs (it has no
way to learn the caller's private path).

The live per-repo stream is truncated at the start of each run. Before that,
`enable_default()` archives the prior run's events to a `history/` dir so past
runs' reviewer findings stay inspectable via `greenlight review-log` long after
the run ends (the PR deliberately carries no findings).

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
_mirror = None  # mirror handle to the per-repo default path, or None

_HISTORY_KEEP = 25  # how many past runs' event logs to retain per repo


def default_path(repo_root: str) -> Path:
    """Per-repo events file under the greenlight state dir.

    Deterministic from the repo root so `greenlight watch` can resolve the same
    file the gate writes to, without the two processes sharing any env.
    """
    from .util import repo_id, state_dir

    return state_dir() / "runs" / repo_id(repo_root) / "events.jsonl"


def history_dir(repo_root: str) -> Path:
    """Where past runs' event logs are archived for `greenlight review-log`."""
    return default_path(repo_root).parent / "history"


def run_logs(repo_root: str) -> list[Path]:
    """Every retained event log for this repo, newest first.

    The live `events.jsonl` (the most recent run) leads, followed by archived
    runs from `history/` in reverse-chronological order. Empty/missing files are
    skipped so the list only contains real runs.
    """
    out: list[Path] = []
    live = default_path(repo_root)
    try:
        if live.exists() and live.stat().st_size > 0:
            out.append(live)
    except OSError:
        pass
    hist = history_dir(repo_root)
    if hist.exists():
        out.extend(sorted(hist.glob("*.jsonl"), reverse=True))
    return out


def _archive(path: Path) -> None:
    """Preserve the previous run's events in history/ before the live stream is
    truncated for a new run, so its findings stay inspectable. Best-effort."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return
        hist = path.parent / "history"
        hist.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        dest = hist / f"{ts}.jsonl"
        i = 1
        while dest.exists():  # two runs in the same second
            dest = hist / f"{ts}-{i}.jsonl"
            i += 1
        dest.write_text(path.read_text())
        stale = sorted(hist.glob("*.jsonl"))[:-_HISTORY_KEEP]
        for f in stale:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def enable_default(repo_root: str, *, truncate: bool = True) -> str:
    """Point the sink at the per-repo default path unless one is already set.

    Returns the active events path. Call this before the pipeline runs (and
    before any emit, so the lazy sink opens against the right file). When the
    caller already set GREENLIGHT_EVENTS (e.g. the pi extension's temp file), it
    wins and the default is left alone.
    """
    global _mirror
    p = default_path(repo_root)
    existing = os.environ.get("GREENLIGHT_EVENTS")
    if existing:
        # A caller (the pi extension) owns the primary stream via its own file.
        # Mirror to the deterministic per-repo path too, so `greenlight watch`
        # can render this run — it can't discover the caller's private path.
        if os.path.abspath(existing) != os.path.abspath(str(p)):
            _archive(p)  # _open_mirror truncates p; keep the prior run first
            _mirror = _open_mirror(p)
        return existing
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if truncate:
            _archive(p)
            p.write_text("")
    except OSError:
        return ""
    os.environ["GREENLIGHT_EVENTS"] = str(p)
    return str(p)


def _open_mirror(path: Path):
    """Open the per-repo mirror sink. Best-effort: None on error.

    Truncate once for a fresh run, then write through an *append* handle (like
    `_handle()`'s default-path sink). A plain "w" handle keeps a fixed offset, so
    if another greenlight process on the same repo truncates this path mid-run
    (its own enable_default `write_text("")`), our stale offset would strand NUL
    bytes into the JSONL `greenlight watch` reads. O_APPEND re-seeks to EOF on
    every write, so a concurrent truncate can't corrupt this stream.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return None


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
    """Append one event to the primary sink and any mirror. No-op when neither is
    active (i.e. GREENLIGHT_EVENTS unset and no mirror opened)."""
    fh = _handle()
    if fh is None and _mirror is None:
        return
    rec = {"ts": time.time(), "type": type, **payload}
    line = json.dumps(rec, default=str) + "\n"
    for sink in (fh, _mirror):
        if sink is None:
            continue
        try:
            sink.write(line)
        except (OSError, ValueError):
            # Never let telemetry break the pipeline.
            pass
