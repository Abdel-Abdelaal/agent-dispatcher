# Design

A thin, serial, quota-aware dispatcher. It isn't a swarm: one run at a time, and
the controller (not the agent) decides what becomes a PR.

## Why this shape

- **Subscription terms.** Claude Code's print mode with a `setup-token` login and
  Codex's `exec` mode on a ChatGPT plan are the supported headless paths. Third-party
  apps that drive a subscription login, or saturate it around the clock, are not.
  Runs are therefore serial and paced, and stop at a share of the weekly quota.
- **Codex stays on the controller host.** Automating a ChatGPT-plan login from
  public-repository GitHub Actions isn't allowed, so `CodexCli` refuses when it sees
  `GITHUB_ACTIONS=true`.
- **No credential in the agent's run.** Each CLI gets an allowlisted environment:
  its own login and the basics, never a GitHub token or an API key. Publishing is a
  separate controller step (the "safe outputs" pattern from gh-aw).

## Flow

- **Intake.** Poll `agent:ready` issues on the configured repos. An issue counts
  only if the owner added that label: anyone can write an issue body.
- **Queue.** SQLite holds tasks and runs. A partial unique index allows at most
  one unfinished run, so a crash or a second process can't start a parallel one.
- **Route.** Claude first, Codex if Claude is over its share or exhausted, park
  otherwise. MiMo and NVIDIA do triage and advisory review, not coding.
- **Run.** The prompt goes on stdin, with the issue inside a delimited untrusted
  block after removing HTML comments, `<details>` blocks and invisible characters.
- **Gate.** The controller reads the diff itself and refuses protected paths,
  binary files, test deletions, paths outside the repository and oversized diffs.
- **Publish.** A draft PR from the App, then the fleet's required checks, then
  Abdel merges.

## States

| Label | Set by | Next |
| --- | --- | --- |
| `agent:ready` | owner only | running, parked |
| `agent:running` | controller | review, parked, failed |
| `agent:parked` | controller (quota) | running, failed |
| `agent:review` | controller (draft PR open) | end |
| `agent:failed` | controller (with reason) | end |

## Protected paths

`.github/`, `.claude/`, `.codex/`, `.mcp.json`, `CODEOWNERS`, `AGENTS.md`,
`CLAUDE.md`, `opencode.json`, `.pre-commit-config.yaml`, `.env*`, lockfiles and
the Gradle wrapper, at any depth and in any letter case. `.claude/` and `.mcp.json`
matter most: a headless run loads a repository's hooks and MCP servers, so one merged
edit there would run code on every later run.

## Phases

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Owner setup: CLI logins, Codex 0.162.1, NVIDIA key, GitHub App | pending |
| 1 | This skeleton, then live Claude runs on `shortlink` docs and test tasks | skeleton done |
| 2 | Controller-run verifiers, repro-first bug check, red-team issue attacks | planned |
| 3 | Codex as second implementer, cross-vendor review, MiMo/GLM triage | planned |
| 4 | Fleet rollout by language, funnel metrics in the signed telemetry | planned |

The full research, with sources, is in the agent platform design report kept
alongside the fleet's planning notes.
