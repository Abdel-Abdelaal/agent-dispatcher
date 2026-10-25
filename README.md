# agent-dispatcher

A serial, quota-aware dispatcher that turns issues Abdel labels into gated draft
PRs, using the official Claude Code and Codex CLIs within their terms.

It's the controller from the
[agent platform design](docs/design.md): GitHub Issues are the queue, labels are
the state machine, one AI run happens at a time, and the controller (not the agent)
decides whether a change is allowed to become a PR. Every PR then goes through the
fleet's normal required checks, and Abdel merges by hand.

## Status

Phase 1 skeleton. The controller runs in **dry-run only**: it polls, queues and plans,
but doesn't call a model or open a PR until the owner setup below is done.

| Piece | State |
| --- | --- |
| Label state machine and SQLite run queue with a single-run lock | built, tested |
| Owner-only intake and issue-text sanitiser | built, tested |
| Provider adapters: `claude -p`, `codex exec`, MiMo and NVIDIA (OpenAI-compatible) | built, tested with fakes; no live call yet |
| Quota guard (weekly share and reset times) | built, tested; headless usage reading unverified |
| Protected-path and diff-size gate | built, tested; not yet wired to a real run |
| GitHub App for draft PRs, Claude `setup-token`, Codex 0.162.1, NVIDIA key | owner setup pending |

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
cp .env.example .env   # then fill in values; never commit .env
```

## Usage

```sh
uv run agent-dispatcher once --dry-run   # one poll cycle, prints the plan
```

Issues count only if the `agent:ready` label was added by `DISPATCHER_OWNER`.
The queue lives in `DISPATCHER_DB_PATH`; see `.env.example` for every setting.

## Validate

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --cov --cov-branch
uv run lint-imports && uv run deptry . && uv run vulture
```

The import contract keeps the decision rules (`states`, `intake`, `gate`, `quota`)
free of network, subprocess and database code, so they're tested as pure functions.
The lockfile never resolves a release younger than 14 days (`exclude-newer`).

## How a task flows

```mermaid
flowchart LR
  Label[Owner adds agent:ready] --> Intake[Intake: owner check + sanitise]
  Intake --> Queue[SQLite queue, one run at a time]
  Queue --> Quota{Quota guard}
  Quota -- exhausted --> Parked[agent:parked until reset]
  Quota -- ok --> Run[Claude or Codex CLI in a sandbox, no GitHub token]
  Run --> Gate{Controller gate: protected paths, diff caps}
  Gate -- refused --> Failed[agent:failed with reason]
  Gate -- passed --> PR[Draft PR, agent:review]
  PR --> CI[Fleet required checks] --> Merge[Abdel merges]
```

## Credits

This code is independent, but the design borrows ideas from these open-source
projects. Thank you to their authors.

- [Cyrus](https://github.com/cyrusagents/cyrus): issue-to-agent sessions with
  per-issue worktrees.
- [OpenAI Symphony](https://github.com/openai/symphony): the poll, claim and
  reconcile loop over an issue tracker.
- [GitHub Agentic Workflows (gh-aw)](https://github.com/github/gh-aw): safe outputs,
  where the agent proposes and a separate step with write access publishes.
- [Codeman](https://github.com/Ark0N/Codeman): quota-aware scheduling of Claude Code
  sessions.

It runs this open-source tool, unmodified, as a separate process:

- [OpenCode](https://github.com/anomalyco/opencode) (MIT): the harness for MiMo and
  NVIDIA runs, through `opencode run` with API keys only.

Why these and not others is in
[ADR 0001](docs/decisions/0001-build-vs-adopt.md).

## Contributing

Changes go through pull requests with Conventional Commit titles. See the
[handbook](https://github.com/Abdel-Abdelaal/.github/tree/main/standards) and
[CONTRIBUTING](https://github.com/Abdel-Abdelaal/.github/blob/main/CONTRIBUTING.md).

## License

[MIT](LICENSE)
