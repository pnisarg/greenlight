"""Classify a changeset as frontend, backend, or mixed from changed paths."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from .config import Routing


@dataclass
class Classification:
    frontend: bool
    backend: bool
    files: list[str]

    @property
    def label(self) -> str:
        if self.frontend and self.backend:
            return "mixed"
        if self.frontend:
            return "frontend"
        if self.backend:
            return "backend"
        return "other"


def _matches(path: str, patterns: list[str]) -> bool:
    # Match against the full posix path and the basename so "*.py" and
    # "backend/*" both behave intuitively.
    base = path.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch(path, pat) or fnmatch(base, pat):
            return True
        # "frontend/*" should match nested files too.
        if pat.endswith("/*") and (path + "/").startswith(pat[:-1]):
            return True
    return False


def classify(files: list[str], routing: Routing) -> Classification:
    fe = any(_matches(f, routing.frontend) for f in files)
    be = any(_matches(f, routing.backend) for f in files)
    return Classification(frontend=fe, backend=be, files=files)
