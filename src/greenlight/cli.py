"""greenlight CLI.

Commands:
  greenlight init [--push-target origin]   set up the gate for this repo
  greenlight run --intent "..."            run the pipeline on the current branch
                                           (explicit path; no push needed)
  greenlight watch                         render the live pipeline card
  greenlight hook --bare ... --work ...    internal: invoked by post-receive
  greenlight doctor                        check environment
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import config, events, gate, gitx, render, worktree
from .pipeline import run_pipeline
from .util import GreenlightError, fail, info, ok, run, step, which


def _cmd_init(args) -> int:
    res = gate.init(args.work or ".", push_target=args.push_target)
    print()
    print("  Push through the gate with:")
    print(f"    git push {gate.REMOTE_NAME} <branch>")
    print()
    print("  Or run the pipeline explicitly:")
    print('    greenlight run --intent "what you set out to do"')
    _maybe_write_default_config(res["repo_root"])
    return 0


def _maybe_write_default_config(root: str) -> None:
    import os

    path = os.path.join(root, config.CONFIG_NAME)
    if os.path.exists(path):
        return
    with open(path, "w") as fh:
        fh.write(_DEFAULT_CONFIG_TOML)
    ok(f"wrote starter config {config.CONFIG_NAME}")


def _read_intent(args) -> str | None:
    """Resolve intent from --intent or --intent-file ('-' = stdin)."""
    if args.intent_file:
        if args.intent_file == "-":
            return sys.stdin.read().strip() or None
        with open(args.intent_file) as fh:
            return fh.read().strip() or None
    return args.intent


def _cmd_run(args) -> int:
    """Explicit-path run: validate the current committed branch in a worktree."""
    root = gitx.main_repo_root(args.work or ".")
    cfg = config.load(root)
    branch = gitx.current_branch(root)
    default_branch = gitx.default_branch(root, cfg.push_target)
    if branch == default_branch:
        raise GreenlightError(
            f"refusing to run on default branch '{branch}'; switch to a feature branch"
        )
    head = gitx.rev_parse(root, "HEAD")
    if not head:
        raise GreenlightError("no commits on this branch")
    base = gitx.merge_base(root, "HEAD", f"{cfg.push_target}/{default_branch}") or ""

    rid = gate._repo_id(root)
    bare = str(gate.bare_path(rid))
    if not (gate.bare_path(rid) / "HEAD").exists():
        raise GreenlightError("gate not initialized; run `greenlight init` first")

    # Publish events to the per-repo default path so `greenlight watch` can find
    # them (honors a caller-set GREENLIGHT_EVENTS, e.g. the pi extension).
    events.enable_default(root)

    # Make the branch + objects available in the bare repo via fetch. fetch does
    # NOT fire the post-receive hook (only pushes do), so the pipeline runs once
    # here inline rather than also being triggered by the gate.
    fetched = run(["git", "fetch", "--force", root,
                   f"refs/heads/{branch}:refs/heads/{branch}"], cwd=bare)
    if not fetched.ok:
        raise GreenlightError(f"could not stage branch into gate: {fetched.err.strip()[:300]}")

    with worktree.checkout(bare, branch, head) as wt:
        passed = run_pipeline(wt, cfg, branch, base, default_branch, _read_intent(args))
    if passed:
        _forward(bare, cfg.push_target, branch)
        _sync_local_branch(root, bare, branch)
        return 0
    fail("pipeline did not pass; nothing forwarded")
    return 1


def _forward(bare: str, push_target: str, branch: str) -> None:
    """Forward the validated branch from the bare gate repo to the real remote.

    The pipeline's fix commits live on the bare repo's branch ref (the worktree
    was created off the bare repo), so forwarding must originate there — the
    bare repo's `origin` remote points at the configured push target's URL.
    """
    step(f"forwarding {branch} -> {push_target}")
    r = run(["git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
            cwd=bare)
    if r.ok:
        ok(f"pushed to {push_target}")
    else:
        fail(f"forward failed: {r.err.strip()[:300]}")


def _sync_local_branch(root: str, bare: str, branch: str) -> None:
    """Fast-forward the user's local branch to include pipeline fix commits.

    The gate may have added lint/review fix commits on top of the user's work.
    Those now live on the bare repo's branch; pull them back so the local branch
    matches what was forwarded. Best-effort: skip if the branch isn't checked
    out cleanly or can't fast-forward.
    """
    bare_head = gitx.rev_parse(bare, branch)
    if not bare_head or bare_head == gitx.rev_parse(root, "HEAD"):
        return
    if gitx.current_branch(root) != branch:
        info(f"gate added fix commits; run `git pull` on {branch} to sync")
        return
    dirty = run(["git", "status", "--porcelain"], cwd=root).out.strip()
    if dirty:
        info(f"gate added fix commits to {branch}; commit/stash and `git pull` to sync")
        return
    ff = run(["git", "merge", "--ff-only", bare_head], cwd=root)
    if ff.ok:
        ok(f"local {branch} fast-forwarded to include fix commits")
    else:
        info(f"gate added fix commits; run `git pull` on {branch} to sync")


def _cmd_hook(args) -> int:
    """Invoked by the post-receive hook inside the bare repo."""
    refs = gate.read_pushed_refs()
    opts = gate.parse_push_options()
    supplied_intent = opts.get("intent")
    # git exports GIT_DIR et al into hooks; drop them so worktree git commands
    # honor their cwd instead of pinning to the bare repo.
    gate.scrub_git_env()
    root = args.work
    cfg = config.load(root)
    default_branch = gitx.default_branch(root, cfg.push_target)
    # Publish events to the per-repo default path so a `greenlight watch` running
    # in the user's terminal can render this server-side run.
    events.enable_default(root)

    overall = 0
    for old_sha, new_sha, refname in refs:
        if not refname.startswith("refs/heads/"):
            continue
        branch = refname[len("refs/heads/") :]
        if gitx.is_zero_sha(new_sha):
            continue  # branch deletion
        step(f"greenlight: validating {branch}")
        # Resolve base against the user's repo (which tracks origin/<default>),
        # since the throwaway worktree won't have those remote refs.
        base = _resolve_base_in_root(root, new_sha, default_branch, cfg.push_target)
        with worktree.checkout(args.bare, branch, new_sha) as wt:
            passed = run_pipeline(wt, cfg, branch, base, default_branch, supplied_intent)
        if passed:
            _forward(args.bare, cfg.push_target, branch)
            _sync_local_branch(root, args.bare, branch)
        else:
            fail(f"{branch} did not pass the gate; not forwarded")
            overall = 1
    return overall


def _resolve_base_in_root(root: str, head_sha: str, default_branch: str, push_target: str) -> str:
    for ref in (f"{push_target}/{default_branch}", default_branch):
        mb = gitx.merge_base(root, head_sha, ref)
        if mb:
            return mb
    return gitx.EMPTY_TREE


def _cmd_watch(args) -> int:
    """Tail the per-repo event stream and render the live pipeline card.

    For the human `git push greenlight` path: the gate runs server-side, so the
    user can't see a UI of their own. Run `greenlight watch` in a second
    terminal; it renders the same card the pi extension shows.
    """
    root = gitx.main_repo_root(args.work or ".")
    path = Path(os.environ.get("GREENLIGHT_EVENTS") or events.default_path(root))
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    step(f"watching {path}")
    if args.once:
        if not path.exists():
            fail("no event stream yet; run the gate first (or pass --work)")
            return 1
        for line in render.render_card(render.state_from(path.read_text()), color):
            print(line)
        return 0

    deadline = time.monotonic() + args.timeout if args.timeout else None
    last_render = ""
    printed_lines = 0
    try:
        while True:
            text = path.read_text() if path.exists() else ""
            state = render.state_from(text)
            lines = render.render_card(state, color)
            block = "\n".join(lines)
            if block != last_render:
                if printed_lines and color:
                    # Redraw in place: move cursor up and clear to end of screen.
                    sys.stdout.write(f"\033[{printed_lines}A\033[J")
                elif printed_lines:
                    print("---")
                print(block)
                sys.stdout.flush()
                printed_lines = len(lines)
                last_render = block
            if state.passed is not None:
                return 0 if state.passed else 1
            if deadline and time.monotonic() > deadline:
                fail("watch timed out before the run finished")
                return 2
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


def _cmd_doctor(args) -> int:
    step("greenlight doctor")
    okk = True
    for tool, required in (("git", True), ("pi", True), ("gh", False)):
        path = which(tool)
        if path:
            ok(f"{tool}: {path}")
        elif required:
            fail(f"{tool}: MISSING (required)")
            okk = False
        else:
            info(f"{tool}: not found (optional — PR creation disabled)")
    return 0 if okk else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="greenlight", description="Local git gate driven by pi.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="set up the gate for this repo")
    pi.add_argument("--work", default=".", help="repo working dir (default: .)")
    pi.add_argument("--push-target", default="origin", help="remote to forward to on pass")
    pi.set_defaults(func=_cmd_init)

    pr = sub.add_parser("run", help="run the pipeline on the current branch")
    pr.add_argument("--work", default=".")
    pr.add_argument("--intent", default=None,
                    help="what you set out to accomplish (the agent that made the change should author this)")
    pr.add_argument("--intent-file", default=None,
                    help="read intent from a file, or '-' for stdin (use for multi-paragraph intent)")
    pr.set_defaults(func=_cmd_run)

    ph = sub.add_parser("hook", help=argparse.SUPPRESS)
    ph.add_argument("--bare", required=True)
    ph.add_argument("--work", required=True)
    ph.set_defaults(func=_cmd_hook)

    pw = sub.add_parser("watch", help="render the live pipeline card from the event stream")
    pw.add_argument("--work", default=".")
    pw.add_argument("--once", action="store_true",
                    help="render the current state once and exit (no follow)")
    pw.add_argument("--interval", type=float, default=0.5,
                    help="poll interval in seconds (default: 0.5)")
    pw.add_argument("--timeout", type=float, default=0,
                    help="give up after N seconds of no completion (0 = wait forever)")
    pw.set_defaults(func=_cmd_watch)

    pd = sub.add_parser("doctor", help="check the environment")
    pd.set_defaults(func=_cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GreenlightError as e:
        fail(str(e))
        return 2
    except KeyboardInterrupt:
        fail("interrupted")
        return 130


_DEFAULT_CONFIG_TOML = """# greenlight configuration. See README for all options.

[greenlight]
max_review_rounds = 3
push_target = "origin"
# model = "anthropic/claude-sonnet-4"   # pi model; empty = pi default
evidence_dir = ".greenlight/evidence"

[checks]
# format_cmd = "ruff format ."
# lint_cmd = "ruff check ."

# Reviewers define what you care about. Each runs as an independent read-only
# agent. Add your own (e.g. a brutal-code-review skill) or focus prompts.
[[reviewers]]
name = "brutal"
focus = "Brutally honest senior review. Real bugs, broken edge cases, race conditions, bad error handling, needless complexity. No style nits."
blocking_severity = "warning"

[[reviewers]]
name = "security"
focus = "Security review: injection, auth gaps, secret leakage, unsafe deserialization, SSRF, path traversal, missing validation."
blocking_severity = "warning"

# [[reviewers]]
# name = "house-style"
# skill = "/path/to/your/review-skill"   # load a pi skill instead of a prompt

[verify]
# Backend test commands (auto-detected if omitted).
# [[verify.backend]]
# name = "unit"
# cmd = "uv run pytest -q"

# Frontend screenshot capture.
[verify.frontend]
url = "http://localhost:3000"
# server_cmd = "npm run dev"
ready_path = "/"

[routing]
# Override which paths count as frontend/backend if the defaults don't fit.
# frontend = ["*.tsx", "frontend/*"]
# backend = ["*.py", "backend/*"]
"""
