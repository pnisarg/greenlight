"""Drive pi non-interactively and parse its output.

`pi -p --mode json` emits JSONL. The final `agent_end` event carries the full
message list; we take the last assistant text. For structured steps (review,
intent) we ask the agent to emit a fenced ```json block and extract it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .util import GreenlightError, run, which

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class AgentResult:
    text: str
    code: int

    def json(self) -> dict | list | None:
        """Best-effort structured payload from the response.

        Tries a fenced ```json block first, then the whole text as JSON.
        Returns None if nothing parses.
        """
        for candidate in self._candidates():
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    def _candidates(self) -> list[str]:
        cands = [m.group(1) for m in _FENCE.finditer(self.text)]
        cands.append(self.text.strip())
        # Also try the last {...} / [...] span in case of prose around it.
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = self.text.find(opener), self.text.rfind(closer)
            if 0 <= i < j:
                cands.append(self.text[i : j + 1])
        return cands


def _last_assistant_text(stdout: str) -> str:
    """Extract the final assistant message text from pi JSONL output."""
    last = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "agent_end":
            for msg in reversed(ev.get("messages", [])):
                if msg.get("role") == "assistant":
                    parts = [
                        c.get("text", "")
                        for c in msg.get("content", [])
                        if c.get("type") == "text"
                    ]
                    return "".join(parts)
        if ev.get("type") in ("turn_end", "message_end"):
            msg = ev.get("message", {})
            if msg.get("role") == "assistant":
                parts = [
                    c.get("text", "")
                    for c in msg.get("content", [])
                    if c.get("type") == "text"
                ]
                if parts:
                    last = "".join(parts)
    return last


class Agent:
    """A configured pi invocation environment."""

    def __init__(self, model: str = "", extra_args: list[str] | None = None):
        self.model = model
        self.extra_args = extra_args or []
        if not which("pi"):
            raise GreenlightError("pi not found on PATH; greenlight needs pi to run")

    def _base(self, read_only: bool, skills: list[str] | None) -> list[str]:
        args = ["pi", "-p", "--mode", "json", "--no-session"]
        if self.model:
            args += ["--model", self.model]
        if read_only:
            # No file mutations possible: reviewers must not touch code.
            args += ["--tools", "read,grep,find,ls,bash"]
        for s in skills or []:
            args += ["--skill", s]
        args += self.extra_args
        return args

    def run(
        self,
        prompt: str,
        cwd: str | Path,
        read_only: bool = False,
        skills: list[str] | None = None,
        timeout: float = 1800,
    ) -> AgentResult:
        args = self._base(read_only, skills)
        r = run(args + [prompt], cwd=cwd, timeout=timeout)
        text = _last_assistant_text(r.out)
        if not text and not r.ok:
            raise GreenlightError(
                f"pi invocation failed ({r.code}): {r.err.strip()[:500]}"
            )
        return AgentResult(text=text, code=r.code)
