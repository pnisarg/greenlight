"""Change-aware verification.

Routes on the diff classification:
  * backend  -> run configured (or auto-detected) test commands; capture logs.
  * frontend -> boot the dev server and capture a screenshot of the change.
  * mixed    -> do both.

All evidence (test logs, screenshots) is written under cfg.evidence_dir in the
worktree and committed, so it travels with the branch and renders on the PR.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from ..agent import Agent
from ..config import Config, VerifyTarget
from ..diff import Classification
from ..util import info, ok, run, step, warn, which
from .types import StepResult


def _auto_backend_targets(work_dir: str) -> list[VerifyTarget]:
    """Best-effort test command detection when none configured."""
    p = Path(work_dir)
    if (p / "pyproject.toml").exists() or (p / "pytest.ini").exists() or (p / "tox.ini").exists():
        if which("uv") and (p / "uv.lock").exists():
            return [VerifyTarget("pytest", "uv run pytest -q")]
        return [VerifyTarget("pytest", "python -m pytest -q")]
    if (p / "go.mod").exists():
        return [VerifyTarget("go test", "go test ./...")]
    if (p / "Cargo.toml").exists():
        return [VerifyTarget("cargo test", "cargo test")]
    if (p / "package.json").exists():
        return [VerifyTarget("npm test", "npm test --silent")]
    return []


def _write_log(evidence_abs: Path, name: str, content: str) -> str:
    safe = name.replace(" ", "-").replace("/", "-")
    path = evidence_abs / f"{safe}.log"
    path.write_text(content)
    return path.name


def _verify_backend(work_dir: str, cfg: Config, evidence_abs: Path, evidence_rel: str) -> StepResult:
    targets = cfg.verify_backend or _auto_backend_targets(work_dir)
    if not targets:
        warn("no backend test command configured or detected; skipping")
        return StepResult(name="verify-backend", passed=True, skipped=True)

    findings_ok = True
    evidence: list[str] = []
    summaries: list[str] = []
    for t in targets:
        info(f"$ {t.cmd}")
        r = run(["bash", "-lc", t.cmd], cwd=work_dir, timeout=t.timeout)
        log = f"$ {t.cmd}\n\n{r.out}\n{r.err}"
        fname = _write_log(evidence_abs, t.name, log)
        evidence.append(f"{evidence_rel}/{fname}")
        if r.ok:
            ok(f"{t.name} passed")
            summaries.append(f"{t.name}: passed")
        else:
            warn(f"{t.name} failed")
            summaries.append(f"{t.name}: FAILED")
            findings_ok = False
    return StepResult(
        name="verify-backend",
        passed=findings_ok,
        summary="; ".join(summaries),
        evidence=evidence,
    )


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _url_host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)


def _capture_screenshot(url: str, out_path: Path) -> bool:
    """Capture a screenshot using whatever headless browser is available."""
    # Prefer playwright if present; else try chromium/chrome headless.
    if which("npx"):
        r = run(
            ["npx", "--yes", "playwright", "screenshot", "--full-page", url, str(out_path)],
            timeout=180,
        )
        if r.ok and out_path.exists():
            return True
    for browser in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if which(browser):
            r = run(
                [browser, "--headless=new", "--no-sandbox", "--hide-scrollbars",
                 f"--screenshot={out_path}", "--window-size=1280,2000", url],
                timeout=120,
            )
            if out_path.exists():
                return True
    return False


def _verify_frontend(cfg: Config, evidence_abs: Path, evidence_rel: str) -> StepResult:
    if not cfg.frontend_server_cmd:
        warn("no frontend server_cmd configured; cannot capture screenshot")
        return StepResult(name="verify-frontend", passed=True, skipped=True)

    host, port = _url_host_port(cfg.frontend_url)
    proc = None
    started = False
    if not _port_open(host, port):
        info(f"$ {cfg.frontend_server_cmd}")
        proc = subprocess.Popen(
            ["bash", "-lc", cfg.frontend_server_cmd],
            cwd=str(evidence_abs.parent.parent),  # worktree root
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        started = True
        for _ in range(60):
            if _port_open(host, port):
                break
            time.sleep(1)
    try:
        if not _port_open(host, port):
            warn("dev server did not come up; skipping screenshot")
            return StepResult(name="verify-frontend", passed=False,
                              summary="dev server failed to start")
        shot = evidence_abs / "screenshot.png"
        if _capture_screenshot(cfg.frontend_url, shot):
            ok(f"captured screenshot -> {evidence_rel}/screenshot.png")
            return StepResult(
                name="verify-frontend",
                passed=True,
                summary=f"screenshot of {cfg.frontend_url}",
                evidence=[f"{evidence_rel}/screenshot.png"],
            )
        warn("no headless browser available; could not capture screenshot")
        return StepResult(name="verify-frontend", passed=True, skipped=True,
                          summary="screenshot unavailable (no browser)")
    finally:
        if started and proc and proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


def run_step(
    agent: Agent,
    work_dir: str,
    cfg: Config,
    cls: Classification,
    commit_fn,
) -> list[StepResult]:
    step(f"verify ({cls.label})")
    evidence_rel = cfg.evidence_dir
    evidence_abs = Path(work_dir) / evidence_rel
    evidence_abs.mkdir(parents=True, exist_ok=True)

    results: list[StepResult] = []
    if cls.backend or cls.label == "other":
        results.append(_verify_backend(work_dir, cfg, evidence_abs, evidence_rel))
    if cls.frontend:
        results.append(_verify_frontend(cfg, evidence_abs, evidence_rel))

    # Commit any evidence so it travels with the branch / shows on the PR.
    commit_fn("chore: add greenlight verification evidence")
    return results
