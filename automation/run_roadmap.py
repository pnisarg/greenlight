#!/usr/bin/env python3
"""Stateless runner for Greenlight's product-roadmap issues.

GitHub is the only tracker. This script owns no durable state: no lock file, no
task database, no background process, no cross-task rebasing. Everything it
needs it reads back from GitHub and git, so an interrupted run leaves nothing to
repair -- the branch and the issue are sufficient for the next attempt.

One invocation does one issue:

    read roadmap issues -> pick the next whose dependencies are merged
    -> update local main -> hand the issue to a *fresh* pi session
    -> verify the session honored the completion contract -> stop

It then stops for a human to review and merge. The runner itself has no merge,
push, or release path by construction (see `_forbid`); the session it starts
pushes its own feature branch, and nothing in the loop merges. That is what
makes it safe to run unattended.

    python3 automation/run_roadmap.py status
    python3 automation/run_roadmap.py next
    python3 automation/run_roadmap.py run          # next ready issue
    python3 automation/run_roadmap.py run 23       # a specific issue
    python3 automation/run_roadmap.py run --dry-run
    python3 automation/run_roadmap.py sync-labels
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "pnisarg/greenlight"
DEFAULT_BASE = "main"
ROADMAP_LABEL = "roadmap"
READY_LABEL = "status:ready"
BLOCKED_LABEL = "status:blocked"
DEFAULT_TIMEOUT = 5400  # 90 minutes; a session past this is hung, not thorough.

_TYPES = "feat|fix|docs|style|refactor|perf|test|chore|ci|revert"
CONVENTIONAL_TITLE = re.compile(rf"^({_TYPES})(\([^)]+\))?!?: .+")
BRANCH_NAME = re.compile(rf"^({_TYPES})/[a-z0-9][a-z0-9._/-]*$")
SESSION_NAME = re.compile(r"^\s*(GL-\d+)\s*:")

_DEPENDENCY = re.compile(r"^\s*[-*]\s*Dependenc(?:y|ies)\s*:\s*(.+?)\s*$", re.M | re.I)
_BRANCH = re.compile(r"^\s*[-*]\s*Branch\s*:\s*`?([^`\s]+)`?\s*$", re.M | re.I)
_PR_TITLE = re.compile(r"^\s*[-*]\s*(?:Draft )?PR title\s*:\s*`(.+?)`\s*$", re.M | re.I)
_ISSUE_REF = re.compile(r"#(\d+)")
_NO_DEPENDENCY = re.compile(r"^(none|n/?a|-|)$", re.I)

# Logs are evidence, not state: they live in .git/ so they are never committed
# and never need a .gitignore entry, and nothing reads them back.
_LOG_SUBDIR = "roadmap-runs"

# Redact before anything reaches a GitHub comment or a log file on disk.
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)(\s*[=:]\s*)(\S+)"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})\b"),
)

# The runner drives git and gh for *reading*, for fast-forwarding the base
# branch, and for issue bookkeeping. The pi session does the committing and
# pushing; the human does the merging. Nothing here can rewrite history or
# publish, and `git merge` is allowed only in its `--ff-only` form.
_FORBIDDEN = (
    ("git", {"push", "rebase", "reset", "cherry-pick", "revert", "tag", "filter-branch"}),
    ("gh", {"release"}),
)


class RunnerError(RuntimeError):
    """Expected, user-facing failure. Printed without a traceback."""


# --------------------------------------------------------------------------- io

_BOLD, _DIM, _GREEN, _YELLOW, _RED, _RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def _color(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stderr.isatty():
        return text
    return f"{code}{text}{_RESET}"


def step(msg: str) -> None:
    print(_color(f"=> {msg}", _BOLD), file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(_color(f"   {msg}", _DIM), file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(_color(f" ok  {msg}", _GREEN), file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(_color(f" !!  {msg}", _YELLOW), file=sys.stderr, flush=True)


def fail(msg: str) -> None:
    print(_color(f" xx  {msg}", _RED), file=sys.stderr, flush=True)


def redact(text: str) -> str:
    out = SECRET_PATTERNS[0].sub(r"\1\2[redacted]", text)
    return SECRET_PATTERNS[1].sub("[redacted]", out)


# ------------------------------------------------------------------- subprocess


@dataclass
class Result:
    argv: list[str]
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def _forbid(argv: Sequence[str]) -> None:
    """Refuse to run anything that could merge, publish, or rewrite history.

    The previous automation attempt failed because it owned too much of the
    lifecycle. This keeps the boundary mechanical rather than aspirational: the
    runner literally cannot merge, so nobody has to audit whether it might.
    """
    if len(argv) < 2:
        return
    tool_name = Path(argv[0]).name
    rendered = shlex.join(list(argv))
    for tool, banned in _FORBIDDEN:
        if tool_name == tool and argv[1] in banned:
            raise RunnerError(f"refusing to run a forbidden command: {rendered}")
    if tool_name == "git" and argv[1] == "merge" and "--ff-only" not in argv:
        raise RunnerError(f"refusing to merge anything but a fast-forward: {rendered}")
    if tool_name == "gh" and list(argv[1:3]) == ["pr", "merge"]:
        raise RunnerError(f"refusing to run a forbidden command: {rendered}")


class Runner:
    """The whole subprocess boundary, injectable in tests. Never uses a shell."""

    def run(
        self,
        argv: Iterable[str],
        *,
        cwd: Path = ROOT,
        timeout: float | None = 120,
        check: bool = True,
    ) -> Result:
        args = [str(a) for a in argv]
        _forbid(args)
        try:
            proc = subprocess.run(
                args, cwd=str(cwd), text=True, capture_output=True, timeout=timeout, check=False
            )
            res = Result(args, proc.returncode, proc.stdout, proc.stderr)
        except FileNotFoundError as exc:
            raise RunnerError(f"{args[0]} is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired:
            res = Result(args, 124, "", f"timed out after {timeout}s")
        if check and not res.ok:
            detail = redact((res.err or res.out).strip())[-2000:]
            raise RunnerError(f"command failed ({res.code}): {shlex.join(args)}\n{detail}")
        return res

    @staticmethod
    def _pump(stream: Any, log: Any) -> None:
        for line in stream:
            sys.stdout.write(line)
            sys.stdout.flush()
            if log is not None:
                log.write(redact(line))
                log.flush()

    def stream(
        self,
        argv: Iterable[str],
        *,
        cwd: Path,
        timeout: float | None,
        log_path: Path | None,
    ) -> int:
        """Run a long child, tee-ing its output to this terminal and a log file.

        The point of a hands-off runner is that you can still watch it, so the
        session's output is not swallowed.

        Output is drained on a thread and the deadline is enforced by waiting on
        the process, not by watching for the next line. A hung session is
        usually a *silent* one, so a deadline checked only when output arrives
        would never fire on the case it exists for.
        """
        args = [str(a) for a in argv]
        _forbid(args)
        log = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            if log is not None:
                log.close()
            raise RunnerError(f"{args[0]} is not installed or not on PATH") from exc

        pump = threading.Thread(target=self._pump, args=(proc.stdout, log), daemon=True)
        pump.start()
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            warn(f"session exceeded {timeout}s; terminating")
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return 124
        finally:
            pump.join(timeout=10)
            if log is not None:
                log.close()


# ------------------------------------------------------------------ issue model


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    branch: str
    pr_title: str
    depends_on: tuple[int, ...]
    contract_error: str = ""

    @property
    def closed(self) -> bool:
        return self.state.upper() == "CLOSED"

    @property
    def session_name(self) -> str:
        m = SESSION_NAME.match(self.title)
        return m.group(1) if m else f"issue-{self.number}"

    def url(self, repo: str) -> str:
        return f"https://github.com/{repo}/issues/{self.number}"


def parse_issue(raw: dict[str, Any]) -> Issue:
    """Read the machine-relevant parts of an issue body.

    A roadmap issue's `## Delivery` section is its build contract. A missing or
    malformed one is recorded as `contract_error` rather than guessed at: the
    runner refuses to invent a branch or PR title, because that is a decision
    the issue was supposed to have already made.
    """
    number = int(raw["number"])
    body = raw.get("body") or ""
    labels = tuple(
        str(lbl["name"]) if isinstance(lbl, dict) else str(lbl) for lbl in raw.get("labels") or []
    )
    problems: list[str] = []

    branch_m = _BRANCH.search(body)
    branch = branch_m.group(1) if branch_m else ""
    if not branch:
        problems.append("no `- Branch: ...` line in the issue body")
    elif not BRANCH_NAME.match(branch):
        problems.append(f"branch {branch!r} is not a conventional `type/slug` name")

    title_m = _PR_TITLE.search(body)
    pr_title = title_m.group(1).strip() if title_m else ""
    if not pr_title:
        problems.append("no `- Draft PR title: `...`` line in the issue body")
    elif not CONVENTIONAL_TITLE.match(pr_title):
        problems.append(f"PR title {pr_title!r} is not a Conventional Commit subject")

    depends: list[int] = []
    for match in _DEPENDENCY.finditer(body):
        text = match.group(1).strip().strip("`")
        if _NO_DEPENDENCY.match(text):
            continue
        refs = [int(n) for n in _ISSUE_REF.findall(text)]
        if not refs:
            problems.append(f"cannot read dependency {text!r} as issue references")
        depends.extend(n for n in refs if n != number)

    return Issue(
        number=number,
        title=str(raw.get("title") or ""),
        state=str(raw.get("state") or "OPEN"),
        labels=labels,
        branch=branch,
        pr_title=pr_title,
        depends_on=tuple(dict.fromkeys(depends)),
        contract_error="; ".join(problems),
    )


DONE, READY, WAITING, BLOCKED, MALFORMED = "done", "ready", "waiting", "blocked", "malformed"


def readiness(issue: Issue, by_number: dict[int, Issue]) -> tuple[str, str]:
    """Classify one issue from GitHub alone. No local state is consulted."""
    if issue.closed:
        return DONE, "closed"
    if BLOCKED_LABEL in issue.labels:
        return BLOCKED, f"labelled {BLOCKED_LABEL}"
    if issue.contract_error:
        return MALFORMED, issue.contract_error
    unmet = []
    for dep in issue.depends_on:
        known = by_number.get(dep)
        if known is None:
            unmet.append(f"#{dep} (not a roadmap issue)")
        elif not known.closed:
            unmet.append(f"#{dep}")
    if unmet:
        return WAITING, "waiting on " + ", ".join(unmet)
    return READY, "dependencies merged"


def next_ready(issues: Sequence[Issue]) -> Issue | None:
    by_number = {i.number: i for i in issues}
    ready = [i for i in issues if readiness(i, by_number)[0] == READY]
    return min(ready, key=lambda i: i.number) if ready else None


# ----------------------------------------------------------------------- prompt

# The executable copy of the prompt contract in docs/roadmap-execution.md. Keep
# the two in sync; the doc is for humans, this is what actually gets sent.
FRESH_PROMPT = """\
Execute the Greenlight product roadmap issue {url} end to end.

Read the issue, docs/product-roadmap.md, repository instructions, relevant
source/tests, and linked dependencies before changing code. Treat the issue's
intent, scope, non-goals, acceptance criteria, and validation as authoritative.
Start from current origin/{base} and create the issue's configured feature branch
`{branch}`.

Within this session:
1. Translate acceptance criteria into failing tests or another observable check.
2. Implement the smallest in-scope change using repository conventions.
3. Run focused validation, then build/lint/typecheck/tests for the affected scope.
4. Perform independent adversarial review for correctness/regressions,
   fail-closed/security behavior, compatibility/migration, test quality, and
   simplicity.
5. Apply every in-scope finding worth fixing now and rerun affected checks.
6. Inspect the final diff and commit with a Conventional Commit subject.
7. Push to {repo} and open a draft PR titled exactly `{pr_title}`, with a body
   containing intent, decisions, validation, residual risks, dependency
   information, and the line `Closes #{number}`.
8. Comment on the issue with the draft PR URL and validation evidence.

Never merge or deploy. Do not weaken Greenlight's fail-closed invariants. If an
unresolved product/architecture decision is not answered by the issue or product
roadmap, make no speculative choice: comment with the exact blocker and stop.
"""

RECOVERY_PROMPT = """\
Continue the Greenlight product roadmap issue {url}. The configured branch
`{branch}` already exists, so previous work is in flight.

Inspect the existing branch and any draft PR before changing anything. Preserve
completed valid work, reproduce the current blocker, finish the issue contract,
rerun validation and independent adversarial review, and update the same draft
PR -- titled exactly `{pr_title}`, with a body containing `Closes #{number}`.
Comment on the issue with the PR URL and validation evidence.

Never merge or deploy. If an unresolved product/architecture decision is not
answered by the issue or docs/product-roadmap.md, comment with the exact blocker
and stop.
"""


def build_prompt(issue: Issue, *, repo: str, base: str, resuming: bool) -> str:
    template = RECOVERY_PROMPT if resuming else FRESH_PROMPT
    return template.format(
        url=issue.url(repo),
        repo=repo,
        base=base,
        branch=issue.branch,
        pr_title=issue.pr_title,
        number=issue.number,
    )


# ------------------------------------------------------------------ orchestrator


@dataclass
class Contract:
    """What the runner checks after the session exits, and what it found."""

    problems: list[str]
    pr_url: str = ""
    pr_number: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


class Orchestrator:
    def __init__(
        self,
        runner: Runner | None = None,
        *,
        repo: str = DEFAULT_REPO,
        base: str = DEFAULT_BASE,
        root: Path = ROOT,
    ):
        self.runner = runner or Runner()
        self.repo = repo
        self.base = base
        self.root = root

    # -- github ------------------------------------------------------------

    def _gh_json(self, argv: Sequence[str]) -> Any:
        res = self.runner.run(["gh", *argv, "--repo", self.repo], cwd=self.root)
        try:
            return json.loads(res.out or "[]")
        except json.JSONDecodeError as exc:
            raise RunnerError(f"gh returned unreadable JSON for {shlex.join(list(argv))}: {exc}")

    def issues(self) -> list[Issue]:
        limit = 200
        raw = self._gh_json(
            [
                "issue", "list",
                "--label", ROADMAP_LABEL,
                "--state", "all",
                "--limit", str(limit),
                "--json", "number,title,state,labels,body",
            ]
        )
        if not isinstance(raw, list) or not raw:
            raise RunnerError(
                f"no issues labelled {ROADMAP_LABEL!r} in {self.repo}; "
                "the roadmap is tracked entirely on GitHub"
            )
        if len(raw) >= limit:
            # Truncation would silently turn a real dependency into an unknown
            # one, so say so rather than computing readiness from a partial list.
            raise RunnerError(
                f"more than {limit} {ROADMAP_LABEL!r} issues; raise the limit "
                "before trusting dependency readiness"
            )
        return sorted((parse_issue(item) for item in raw), key=lambda i: i.number)

    def comment(self, number: int, body: str) -> None:
        self.runner.run(
            ["gh", "issue", "comment", str(number), "--repo", self.repo, "--body", redact(body)],
            cwd=self.root,
        )

    def label(self, number: int, *, add: str = "", remove: str = "") -> None:
        argv = ["gh", "issue", "edit", str(number), "--repo", self.repo]
        if add:
            argv += ["--add-label", add]
        if remove:
            argv += ["--remove-label", remove]
        self.runner.run(argv, cwd=self.root)

    # -- git ---------------------------------------------------------------

    def _git(self, *argv: str, check: bool = True) -> Result:
        return self.runner.run(["git", *argv], cwd=self.root, check=check)

    def git_dir(self) -> Path:
        res = self._git("rev-parse", "--absolute-git-dir")
        return Path(res.out.strip())

    def prepare_base(self) -> str:
        """Land on a clean, current base branch. Untracked files are fine."""
        dirty = self._git("status", "--porcelain", "-uno").out.strip()
        if dirty:
            raise RunnerError(
                "working tree has uncommitted tracked changes; commit or stash them first:\n"
                + dirty
            )
        self._git("switch", self.base)
        # Fetch explicitly, then fast-forward only. An implicit `git pull` could
        # create a merge commit on the base branch; this cannot.
        self._git("fetch", "origin", self.base)
        head = self._git("rev-parse", "HEAD").out.strip()
        remote = self._git("rev-parse", f"origin/{self.base}").out.strip()
        if head != remote:
            merge_base = self._git("merge-base", "HEAD", f"origin/{self.base}").out.strip()
            if merge_base != head:
                raise RunnerError(
                    f"local {self.base} is not a fast-forward of origin/{self.base} "
                    "(diverged, or ahead with unpushed commits); reconcile it by hand"
                )
            self._git("merge", "--ff-only", f"origin/{self.base}")
        return remote

    def branch_exists(self, branch: str) -> bool:
        local = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if local.ok:
            return True
        remote = self._git("ls-remote", "--heads", "origin", branch, check=False)
        return bool(remote.ok and remote.out.strip())

    # -- the session -------------------------------------------------------

    def session_argv(self, issue: Issue, prompt: str) -> list[str]:
        # `--approve` trusts this repo's own project-local pi files, which a
        # non-interactive session cannot be prompted about.
        return ["pi", "-p", "--approve", "--name", issue.session_name, prompt]

    def log_path(self, issue: Issue) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        return self.git_dir() / _LOG_SUBDIR / f"{issue.number}-{stamp}.log"

    def verify_contract(self, issue: Issue) -> Contract:
        """Check GitHub and git for the evidence the session was asked to leave.

        The session's own summary is not evidence. Only a pushed branch and a
        correctly shaped draft PR count.
        """
        problems: list[str] = []
        remote = self._git("ls-remote", "--heads", "origin", issue.branch, check=False)
        if not (remote.ok and remote.out.strip()):
            problems.append(f"branch `{issue.branch}` was never pushed to origin")

        raw = self._gh_json(
            [
                "pr", "list",
                "--head", issue.branch,
                "--state", "open",
                "--json", "number,url,title,body,isDraft,baseRefName",
            ]
        )
        prs = raw if isinstance(raw, list) else []
        if not prs:
            problems.append(f"no open pull request for `{issue.branch}`")
            return Contract(problems)

        pr = prs[0]
        pr_url = str(pr.get("url") or "")
        pr_number = int(pr.get("number") or 0)
        if len(prs) > 1:
            problems.append(f"{len(prs)} open pull requests for `{issue.branch}`; expected one")
        if not pr.get("isDraft"):
            problems.append(f"PR #{pr_number} is not a draft")
        base_ref = str(pr.get("baseRefName") or "")
        if base_ref != self.base:
            # A PR stacked on another feature branch would merge unreviewed work
            # along with it, which is exactly what the no-stacking rule forbids.
            problems.append(f"PR #{pr_number} targets {base_ref!r}, not {self.base!r}")
        actual_title = str(pr.get("title") or "")
        if actual_title != issue.pr_title:
            problems.append(
                f"PR title is {actual_title!r}; the issue configured {issue.pr_title!r}"
            )
        body = str(pr.get("body") or "")
        if not re.search(rf"\b(closes|fixes|resolves)\s+#{issue.number}\b", body, re.I):
            problems.append(f"PR body does not contain `Closes #{issue.number}`")
        return Contract(problems, pr_url=pr_url, pr_number=pr_number)

    def issue_mentions_pr(self, number: int, pr_number: int) -> bool:
        """True when some issue comment already links the PR, by ref or by URL."""
        raw = self._gh_json(["issue", "view", str(number), "--json", "comments"])
        comments = raw.get("comments") or [] if isinstance(raw, dict) else []
        needle = re.compile(rf"(#{pr_number}\b|/pull/{pr_number}\b)")
        return any(needle.search(str(c.get("body") or "")) for c in comments)

    # -- commands ----------------------------------------------------------

    def cmd_status(self) -> int:
        issues = self.issues()
        by_number = {i.number: i for i in issues}
        counts: dict[str, int] = {}
        for issue in issues:
            state, detail = readiness(issue, by_number)
            counts[state] = counts.get(state, 0) + 1
            mark = {DONE: " ok ", READY: " => ", WAITING: "  . ", BLOCKED: " !! ",
                    MALFORMED: " xx "}[state]
            print(f"{mark} #{issue.number:<4} {state:<9} {issue.title}")
            if state in (WAITING, BLOCKED, MALFORMED):
                print(f"      {detail}")
        # The table goes to stdout so it can be piped; keep it ordered against
        # the stderr summary below even when stdout is a block-buffered pipe.
        sys.stdout.flush()
        summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        info(summary)
        nxt = next_ready(issues)
        if nxt:
            ok(f"next: #{nxt.number} {nxt.title}")
        elif counts.get(WAITING) or counts.get(BLOCKED) or counts.get(MALFORMED):
            warn("nothing ready; every open issue is waiting, blocked, or malformed")
        else:
            ok("roadmap complete")
        return 0

    def cmd_next(self) -> int:
        nxt = next_ready(self.issues())
        if not nxt:
            warn("no roadmap issue is ready")
            return 3
        print(nxt.url(self.repo))
        sys.stdout.flush()
        ok(f"#{nxt.number} {nxt.title}  branch {nxt.branch}")
        return 0

    def cmd_sync_labels(self, *, dry_run: bool = False) -> int:
        issues = self.issues()
        by_number = {i.number: i for i in issues}
        changed = 0
        for issue in issues:
            if issue.closed:
                continue
            state, _ = readiness(issue, by_number)
            has = READY_LABEL in issue.labels
            want = state == READY
            if has == want:
                continue
            changed += 1
            verb = "add" if want else "remove"
            info(f"{'would ' if dry_run else ''}{verb} {READY_LABEL} on #{issue.number}")
            if not dry_run:
                self.label(issue.number, **{"add" if want else "remove": READY_LABEL})
        ok(f"{changed} label change(s)" + (" (dry run)" if dry_run else ""))
        return 0

    def cmd_run(
        self,
        number: int | None = None,
        *,
        dry_run: bool = False,
        timeout: float | None = DEFAULT_TIMEOUT,
        sync: bool = True,
    ) -> int:
        issues = self.issues()
        by_number = {i.number: i for i in issues}

        if number is None:
            issue = next_ready(issues)
            if issue is None:
                warn("no roadmap issue is ready; run `status` to see why")
                return 3
        else:
            issue = by_number.get(number)
            if issue is None:
                raise RunnerError(f"#{number} is not an open or closed {ROADMAP_LABEL} issue")
            state, detail = readiness(issue, by_number)
            if state != READY:
                raise RunnerError(f"#{number} is {state}: {detail}")

        deps = ", ".join("#" + str(d) for d in issue.depends_on) or "nothing"
        step(f"#{issue.number} {issue.title}")
        info(f"branch      {issue.branch}")
        info(f"pr title    {issue.pr_title}")
        info(f"depends on  {deps}")

        if dry_run:
            base_ref = "(not touched)"
            resuming = self.branch_exists(issue.branch)
        else:
            base_ref = self.prepare_base()
            ok(f"base        origin/{self.base} at {base_ref[:12]}")
            resuming = self.branch_exists(issue.branch)
        if resuming:
            warn(f"branch `{issue.branch}` already exists; sending the recovery prompt")

        prompt = build_prompt(issue, repo=self.repo, base=self.base, resuming=resuming)
        argv = self.session_argv(issue, prompt)

        if dry_run:
            step("dry run; the session would receive")
            print(prompt)
            sys.stdout.flush()
            info(shlex.join(argv[:-1] + ["<prompt>"]))
            return 0

        log = self.log_path(issue)
        step(f"pi session {issue.session_name}  (log {log})")
        code = self.runner.stream(argv, cwd=self.root, timeout=timeout, log_path=log)
        if code == 124:
            warn(f"session hit the {timeout}s ceiling")
        elif code != 0:
            warn(f"session exited {code}")
        else:
            ok("session finished")

        step("verifying the completion contract")
        contract = self.verify_contract(issue)
        if not contract.ok:
            for problem in contract.problems:
                fail(problem)
            self._record_block(issue, contract, session_code=code, log=log)
            return 1

        for line in (
            f"draft PR    {contract.pr_url}",
            f"branch      {issue.branch} pushed",
            f"title       {issue.pr_title}",
            f"closes      #{issue.number}",
        ):
            ok(line)

        if not self.issue_mentions_pr(issue.number, contract.pr_number):
            info("session did not comment on the issue; recording the PR link")
            self.comment(issue.number, f"Draft PR: {contract.pr_url}")

        if sync:
            self.cmd_sync_labels()

        step("stopping for human review")
        info(f"review and merge {contract.pr_url}")
        info("then: python3 automation/run_roadmap.py run   # picks the next ready issue")
        return 0

    def _record_block(
        self, issue: Issue, contract: Contract, *, session_code: int, log: Path
    ) -> None:
        """Park a failed attempt on GitHub so a later run does not retry blindly."""
        lines = [
            "The automated roadmap runner could not verify this issue's completion contract.",
            "",
            f"- pi session exit code: `{session_code}`",
            f"- local session log: `{log}`",
            "",
            "Unmet contract requirements:",
            *(f"- {p}" for p in contract.problems),
            "",
            f"Labelled `{BLOCKED_LABEL}`. Resolve the blocker, remove the label, and rerun "
            "`python3 automation/run_roadmap.py run` to resume from the existing branch.",
        ]
        try:
            self.comment(issue.number, "\n".join(lines))
            if BLOCKED_LABEL not in issue.labels:
                self.label(issue.number, add=BLOCKED_LABEL)
            warn(f"#{issue.number} labelled {BLOCKED_LABEL}")
        except RunnerError as exc:
            warn(f"could not record the blocker on #{issue.number}: {exc}")


# -------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_roadmap",
        description="Run one Greenlight roadmap issue in a fresh pi session, then stop.",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo (default {DEFAULT_REPO})")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"base branch (default {DEFAULT_BASE})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show every roadmap issue and what it is waiting on")
    sub.add_parser("next", help="print the next ready issue URL")

    run = sub.add_parser("run", help="implement one ready issue and open a draft PR")
    run.add_argument("issue", nargs="?", type=int, help="issue number (default: next ready)")
    run.add_argument("--dry-run", action="store_true", help="show the plan and prompt only")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                     help=f"session ceiling in seconds (default {DEFAULT_TIMEOUT}, 0 disables)")
    run.add_argument("--no-sync-labels", action="store_true", help="do not touch status labels")

    sync = sub.add_parser("sync-labels", help=f"reconcile {READY_LABEL} with dependency state")
    sync.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    orch = Orchestrator(repo=args.repo, base=args.base)
    try:
        if args.command == "status":
            return orch.cmd_status()
        if args.command == "next":
            return orch.cmd_next()
        if args.command == "sync-labels":
            return orch.cmd_sync_labels(dry_run=args.dry_run)
        return orch.cmd_run(
            args.issue,
            dry_run=args.dry_run,
            timeout=args.timeout if args.timeout and args.timeout > 0 else None,
            sync=not args.no_sync_labels,
        )
    except RunnerError as exc:
        fail(str(exc))
        return 2
    except KeyboardInterrupt:
        fail("interrupted; nothing local to repair")
        return 130


if __name__ == "__main__":
    sys.exit(main())
