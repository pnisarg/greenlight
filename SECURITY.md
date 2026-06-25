# Security Policy

## Supported versions

greenlight is pre-1.0 and experimental. Only the latest release on `main`
receives fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use
GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (Security → Report a vulnerability), or contact the
maintainer directly.

Please include reproduction steps and the affected version. You will get an
acknowledgement, and a fix or mitigation timeline once the report is triaged.

## Notes on the threat model

greenlight runs AI agents (`pi`) over your code and executes configured
commands (lint, tests, dev servers) in a throwaway worktree. Treat the intent
text and any agent-produced output as untrusted input to downstream prompts.
Reviewers run read-only (no edit/write tools); only the dedicated fix step can
modify code. Never put secrets in `.greenlight.toml` or in intent text.
