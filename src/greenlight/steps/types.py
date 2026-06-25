"""Shared data types for pipeline steps."""
from __future__ import annotations

from dataclasses import dataclass, field

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


@dataclass
class Finding:
    severity: str  # info | warning | error
    file: str
    line: int | None
    description: str
    reviewer: str = ""

    def blocks(self, threshold: str) -> bool:
        return _SEVERITY_RANK.get(self.severity, 1) >= _SEVERITY_RANK.get(threshold, 1)

    def render(self) -> str:
        loc = self.file + (f":{self.line}" if self.line else "")
        tag = f"[{self.reviewer}] " if self.reviewer else ""
        return f"{tag}{self.severity.upper()} {loc} — {self.description}"


@dataclass
class StepResult:
    name: str
    passed: bool
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # relative paths in worktree
    skipped: bool = False
