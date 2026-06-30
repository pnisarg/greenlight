"""greenlight configuration.

Loaded from .greenlight.toml at the repo root, layered over built-in defaults.
The novel part of greenlight lives here: the `reviewers` list lets you declare
exactly what you care about in review (brutal code review, security, etc.), and
the verify/routing config makes verification change-type aware.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".greenlight.toml"


@dataclass
class Reviewer:
    name: str
    # A pi skill to load for this reviewer (e.g. a "brutal code review" skill),
    # or an inline focus prompt. At least one should be set.
    skill: str | None = None
    focus: str = ""
    # Findings at or above this severity block the gate. error|warning|info.
    blocking_severity: str = "warning"
    enabled: bool = True


@dataclass
class VerifyTarget:
    """A verification command bound to a change class."""

    name: str
    cmd: str  # shell command, run in the worktree
    timeout: int = 1200


@dataclass
class Routing:
    # Glob patterns (fnmatch over posix paths) that classify a changed file.
    frontend: list[str] = field(
        default_factory=lambda: [
            "*.tsx", "*.jsx", "*.ts", "*.js", "*.vue", "*.svelte",
            "*.css", "*.scss", "*.html",
            "frontend/*", "web/*", "ui/*", "client/*", "src/components/*",
        ]
    )
    backend: list[str] = field(
        default_factory=lambda: [
            "*.py", "*.go", "*.rs", "*.java", "*.rb",
            "backend/*", "server/*", "api/*", "services/*",
        ]
    )


@dataclass
class Config:
    reviewers: list[Reviewer]
    routing: Routing
    # Verification commands keyed by class: "backend", "frontend".
    verify_backend: list[VerifyTarget]
    # Frontend screenshot: a URL to capture and the dev-server command to boot.
    frontend_url: str
    frontend_server_cmd: str
    frontend_server_ready_path: str
    # CI monitoring (post-PR). The real remote CI is the authoritative test
    # signal — it has installed deps, services, and caching the throwaway
    # worktree can't reproduce. When enabled, the gate polls the PR's check
    # rollup after opening it, auto-fixes failures (intent-preserving) up to
    # ci_max_fix_rounds, and only reports green once CI is green. GitHub only.
    ci_enabled: bool
    ci_provider: str  # "github"
    # Idle timeout (s) for the whole CI watch, independent of run_timeout. Each
    # auto-fix re-arms it. 0 = wait forever (until CI settles).
    ci_timeout: int
    # Intent-preserving auto-fix attempts on CI failure before giving up.
    ci_max_fix_rounds: int
    # Only gate on these check names (matched against check/workflow name).
    # Empty = gate on every check the PR reports.
    ci_required_checks: list[str]
    # Format/lint commands. Run before review; failures here are auto-fixable.
    format_cmd: str
    lint_cmd: str
    # Where verification evidence (screenshots, test logs) is written, relative
    # to the worktree. Committed so it renders on the PR.
    evidence_dir: str
    # Max review->fix iterations before giving up.
    max_review_rounds: int
    # Wall-clock budget for the whole run, in seconds. Clamps every agent/
    # subprocess call to the time remaining and aborts the pipeline gracefully
    # once exhausted, so a degraded LLM gateway can't stall a run for hours.
    # 0 disables the cap.
    run_timeout: int
    # The agent model passed to pi (empty = pi default).
    model: str
    push_target: str  # remote the gate forwards to on pass


def _default_reviewers() -> list[Reviewer]:
    return [
        Reviewer(
            name="brutal",
            focus=(
                "You are a brutally honest senior engineer doing code review. "
                "Find real bugs, broken edge cases, race conditions, incorrect "
                "error handling, leaky abstractions, and needless complexity. "
                "No praise, no nitpicks about style. Only substantive issues "
                "that a careful reviewer would block a PR on."
            ),
            blocking_severity="warning",
        ),
        Reviewer(
            name="security",
            focus=(
                "You are a security reviewer. Look for injection, auth/authz "
                "gaps, secret leakage, unsafe deserialization, SSRF, path "
                "traversal, missing input validation, and insecure defaults "
                "introduced by this change. Flag only concrete, exploitable "
                "issues anchored to changed lines."
            ),
            blocking_severity="warning",
        ),
    ]


def default_config() -> Config:
    return Config(
        reviewers=_default_reviewers(),
        routing=Routing(),
        verify_backend=[],  # auto-detected at runtime if empty (see verify step)
        frontend_url="http://localhost:3000",
        frontend_server_cmd="",
        frontend_server_ready_path="/",
        ci_enabled=False,
        ci_provider="github",
        ci_timeout=2700,
        ci_max_fix_rounds=2,
        ci_required_checks=[],
        format_cmd="",
        lint_cmd="",
        evidence_dir=".greenlight/evidence",
        max_review_rounds=3,
        run_timeout=1200,
        model="",
        push_target="origin",
    )


def _coerce_reviewers(raw: list[dict]) -> list[Reviewer]:
    out = []
    for r in raw:
        out.append(
            Reviewer(
                name=str(r["name"]),
                skill=r.get("skill"),
                focus=str(r.get("focus", "")),
                blocking_severity=str(r.get("blocking_severity", "warning")),
                enabled=bool(r.get("enabled", True)),
            )
        )
    return out


def _coerce_verify(raw: list[dict]) -> list[VerifyTarget]:
    return [
        VerifyTarget(
            name=str(v["name"]),
            cmd=str(v["cmd"]),
            timeout=int(v.get("timeout", 1200)),
        )
        for v in raw
    ]


def load(repo_root: str | Path) -> Config:
    """Load config from repo, layering file values over defaults."""
    cfg = default_config()
    path = Path(repo_root) / CONFIG_NAME
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text())

    if "reviewers" in data:
        rv = _coerce_reviewers(data["reviewers"])
        if rv:
            cfg.reviewers = rv

    routing = data.get("routing", {})
    if "frontend" in routing:
        cfg.routing.frontend = list(routing["frontend"])
    if "backend" in routing:
        cfg.routing.backend = list(routing["backend"])

    verify = data.get("verify", {})
    if "backend" in verify:
        cfg.verify_backend = _coerce_verify(verify["backend"])
    fe = verify.get("frontend", {})
    cfg.frontend_url = str(fe.get("url", cfg.frontend_url))
    cfg.frontend_server_cmd = str(fe.get("server_cmd", cfg.frontend_server_cmd))
    cfg.frontend_server_ready_path = str(
        fe.get("ready_path", cfg.frontend_server_ready_path)
    )

    ci = data.get("ci", {})
    cfg.ci_enabled = bool(ci.get("enabled", cfg.ci_enabled))
    cfg.ci_provider = str(ci.get("provider", cfg.ci_provider))
    cfg.ci_timeout = int(ci.get("timeout", cfg.ci_timeout))
    cfg.ci_max_fix_rounds = int(ci.get("max_fix_rounds", cfg.ci_max_fix_rounds))
    if "required_checks" in ci:
        cfg.ci_required_checks = [str(c) for c in ci["required_checks"]]

    checks = data.get("checks", {})
    cfg.format_cmd = str(checks.get("format_cmd", cfg.format_cmd))
    cfg.lint_cmd = str(checks.get("lint_cmd", cfg.lint_cmd))

    gen = data.get("greenlight", {})
    cfg.evidence_dir = str(gen.get("evidence_dir", cfg.evidence_dir))
    cfg.max_review_rounds = int(gen.get("max_review_rounds", cfg.max_review_rounds))
    cfg.run_timeout = int(gen.get("run_timeout", cfg.run_timeout))
    cfg.model = str(gen.get("model", cfg.model))
    cfg.push_target = str(gen.get("push_target", cfg.push_target))
    return cfg
