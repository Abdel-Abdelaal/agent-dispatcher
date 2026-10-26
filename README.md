# agent-dispatcher

A serial, quota-aware dispatcher that turns issues Abdel labels into gated draft
PRs, using the official Claude Code and Codex CLIs within their terms.

It's the controller from the
[agent platform design](docs/design.md): GitHub Issues are the queue, labels are
the state machine, one AI run happens at a time, and the controller (not the agent)
decides whether a change is allowed to become a PR. Every PR then goes through the
fleet's normal required checks, and Abdel merges by hand.

## Status

Phase 3 routing on the laptop, which stands in for the VPS until the move. `once`
runs the next task with a team; `once --dry-run` only plans.

## Who does what

Claude and Codex are the strongest models with the smallest allowances, so they
only think: short, read-only plan and review steps. MiMo and NVIDIA have the
largest allowances, so they do the token-heavy implementation.

| Step | Role | Provider, in order of preference | Harness |
| --- | --- | --- | --- |
| Triage | cheap screen | MiMo, else NVIDIA (chat API, JSON verdict) | HTTP |
| Plan | thinker, read-only | Claude, else Codex | Claude Code / Codex CLI |
| Implement | worker | MiMo, else NVIDIA, else a thinker as a last resort | Claude Code on MiMo's Anthropic API / OpenCode on NVIDIA's OpenAI API |
| Gate | controller | protected paths, diff size, test deletions, symlinks | Git |
| Review | the other thinker, read-only | Codex if Claude planned, and the reverse | Codex CLI / Claude Code |
| Repair | worker | one round on the reviewer's findings, then gate and review again | as above |

A provider that reports its limit is parked until the time it gives (Codex prints
one) or for `DISPATCHER_PARK_HOURS`, and the next run forms a team without it.
Without any thinker the task waits. NVIDIA joins once `DISPATCHER_NVIDIA_API_KEY`
is set.

| Piece | State |
| --- | --- |
| Label state machine, SQLite queue, single-run lock, owner-only intake | built, tested |
| Live run: two clones, gate on the clean one, draft PR through `gh` | built, live-tested |
| Claude (plan, review) on its CLI login | built, live-tested |
| Codex (plan, review) on its ChatGPT login | built, tested with fakes |
| MiMo (triage, implement) through Claude Code | built, harness live-tested |
| NVIDIA (implement) through OpenCode | built; harness live-tested on MiMo's OpenAI API, needs a key |
| GitHub App for PRs | waits for the VPS move; the laptop uses the owner's `gh` login |

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
cp .env.example .env   # then fill in values; never commit .env
```

## Usage

```sh
uv run agent-dispatcher once --dry-run   # one poll cycle, prints the plan
uv run agent-dispatcher once             # also runs the next task
```

Issues count only if the `agent:ready` label was added by `DISPATCHER_OWNER`.
The queue lives in `DISPATCHER_DB_PATH`; see `.env.example` for every setting.

A live run:

1. Labels the issue `agent:running`.
2. Clones the repository twice under `DISPATCHER_WORK_ROOT`.
3. Runs the provider in the first clone with the issue as untrusted data.
4. Applies only the resulting diff to the second, clean clone and gates it there.
5. Pushes from the clean clone with Git hooks disabled, opens a draft PR and
    labels the issue `agent:review`.

A refused or failed run gets `agent:failed` and a comment with the reason. An
exhausted provider gets `agent:parked` and is retried after `DISPATCHER_PARK_HOURS`.

The agent never pushes or holds a GitHub token. It can write anything in its own
clone, `.git/hooks` and `.git/config` included, so nothing is pushed from there.
Claude runs with `--setting-sources local`. That ignores user settings (which may
point Claude Code at another provider) and the repository's own
`.claude/settings.json`, whose hooks would otherwise run. Until the agent runs in
a sandbox it gets no Bash: it edits files, and CI runs the tests.

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
