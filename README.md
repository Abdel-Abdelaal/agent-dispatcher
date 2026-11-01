# agent-dispatcher

Already installed? Start with [Using the agent team](docs/using-the-team.md):
queue a task, inspect progress, review a PR, pause the scheduler and understand
provider fallback and the current quality limits.

## Provider fallback

The host can exclude an exhausted provider with
`DISPATCHER_DISABLED_PROVIDERS=claude`. Codex then plans and reviews while MiMo
implements. If Codex is also parked, the owner can enable
`DISPATCHER_ALLOW_WORKER_THINKING=true`: MiMo performs separate planning,
implementation and read-only review calls. This is same-provider review with
less independence, not a cross-provider quality guarantee. The telemetry records
the actual provider for every stage. Protected paths, diff limits, tests, turn
caps and draft-only publication still apply. No fallback purchases capacity.
When all configured providers are unavailable, the task waits for quota reset.

MiMo uses Claude Code as a local harness with the configured MiMo gateway; this
does not select the Claude subscription provider. The two providers have separate
quota identities in the controller.

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

| Step | Role | Provider, in order of preference |
| --- | --- | --- |
| Triage | cheap screen; also rates the task easy, medium or hard | MiMo, else NVIDIA (chat API, JSON verdict) |
| Plan | thinker, read-only; re-rates the task on its first line | Claude, else Codex |
| Implement | worker | MiMo (Claude Code harness), else NVIDIA (OpenCode), else a thinker (Sonnet 5.5 / gpt-6.1-sol, medium) |
| Gate | controller | protected paths, diff size, test deletions, symlinks |
| Review | the other thinker, read-only | Codex if Claude planned, and the reverse |
| Repair | worker | one round on the reviewer's findings, then gate and review again |

### Thinking follows the task's difficulty

Conserving the thinkers' small allowances comes first, so the frontier models only
run on tasks that prove hard, and nothing goes above high effort.

| Level | Claude | Codex |
| --- | --- | --- |
| easy | `claude-sonnet-5-5`, low | `gpt-6.1-sol`, low |
| medium (also the default without triage) | `claude-sonnet-5-5`, medium | `gpt-6.1-sol`, medium |
| hard | `claude-opus-5-5`, high | `gpt-6-astra`, high |

The level can move during a run:

1. Triage sets the first level.
2. The planner rates the task on its first line. If it rates it harder than the
    level it planned at, it plans again at the new level. If it rates it easier, the
    review drops to the cheaper setting.
3. Each rejected review raises the level one step for the repair and the next
    review.

The level used is noted in the PR and in an `Agent-Level` commit trailer. The ladder
is the `DISPATCHER_THINKING_LADDER` setting (JSON), and the dispatcher passes every
model and effort explicitly, so changing the Codex app's `config.toml` doesn't
change the dispatcher.

A provider that reports its limit is parked until the time it gives (Codex prints
one) or for `DISPATCHER_PARK_HOURS`, and the next run forms a team without it.
Without a thinker the task waits unless the owner enables worker-only thinking
as described above. NVIDIA joins once `DISPATCHER_NVIDIA_API_KEY` is set.

Any other model with an OpenAI-compatible API joins through
`DISPATCHER_EXTRA_PROVIDERS`, with no code: OpenRouter, Groq, DeepSeek, Gemini, a
local Ollama or LM Studio server, or a LiteLLM proxy for everything else. Extras
run through MiMo's locked-down OpenCode harness and come after the built-in
providers of their role. See [extra providers](docs/extra-providers.md).

| Piece | State |
| --- | --- |
| Label state machine, SQLite queue, single-run lock, owner-only intake | built, tested |
| Live run: two clones, gate on the clean one, draft PR through `gh` | built, live-tested |
| Claude (plan, review) on its CLI login | built, live-tested |
| Codex (plan, review) on its ChatGPT login | built, tested with fakes |
| MiMo (triage, implement) through Claude Code | built, harness live-tested |
| NVIDIA (implement) through OpenCode | built; harness live-tested on MiMo's OpenAI API, needs a key |
| Extra providers (any OpenAI-compatible API) through OpenCode | built, tested with fakes |
| [Sandbox](docs/sandbox.md): models run tests in locked-down containers; checks before review | built, tested with fakes and by hand on the VPS |
| GitHub App for PRs | waits for the VPS move; the laptop uses the owner's `gh` login |

### Keeping token use down

- Each run gets only the tools it needs (`--tools`): thinkers get Read, Glob and Grep,
  and workers add Edit and Write. No MCP servers load (`--strict-mcp-config`).
  Unused tool definitions aren't resent on every turn. On one paired MiMo run this
  cut input tokens from 30,263 to 7,315, with the same result.
- Turn caps follow the task's level: 10/15 turns (thinking/working) for easy tasks,
  15/25 for medium, and 25/40 for hard.
- Fixed instructions come first in every prompt and the issue last, so provider
  caching can reuse the prefix.
- `DISPATCHER_SKIP_PLAN_FOR_EASY_DOCS=true` is an experiment, off by default. It sends
  easy documentation tasks straight to the worker, and the review still checks them.
  Compare telemetry before keeping it on.

### Which repositories

`DISPATCHER_REPOS=auto` watches every public, non-fork, non-archived repository the
owner has, discovered on each cycle, so a new repository joins without a settings
change. Private repositories are never discovered. The controller's own repository
is always excluded (`DISPATCHER_EXCLUDE_REPOS`), so no agent can edit its gate. A
repository also needs the `agent:*` labels before tasks can be queued there.

### CI repair

When no task is waiting, a cycle looks at open agent PRs (branches named
`agent/<issue>-run-<id>`). The checks that count are the required ones plus the
fleet's shared `hooks / Hooks` and `lint / Lint (all languages)`, which few
rulesets require but every merge needs green. If all of those have finished and one
failed, a thinker (the planner) gets the extracted error lines of each failing
job's log (up to three jobs, required ones first) for one repair. Workers
can't run linters, and a CI log names exact files and lines. The fix goes through the same gate on a clean clone of the PR branch and a
read-only review by a thinker, then is pushed to that branch, and the result is
commented on the PR. Advisory checks (`review / ai`) and other optional checks
never trigger a repair. A PR is
repaired at most once: if that doesn't fix it, a human should look.

### Telemetry

Every attempt (triage, plan, implement, gate, review, repair, publish, and providers
left out of a team) is written as one bounded JSON record to
`runs/telemetry/attempts-YYYY-MM.jsonl`. The fields are listed in
[docs/telemetry.md](docs/telemetry.md). Prompts are only hashed, and usage is
recorded exactly as each CLI reports it, or `null` when unknown. The platform's
telemetry tooling signs and publishes the records; this repository never does.

### Driving it from Claude

`skills/agent-dispatcher/SKILL.md` is an [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)
that teaches any Claude Code session to file tasks, read `agent-dispatcher status`
and the run log, explain failures and change models or repositories safely.
Install it by copying the folder to `~/.claude/skills/`.

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
uv run agent-dispatcher status           # waiting tasks, parked models, recent runs
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
Patch extraction also ignores its Git config, HEAD and index: the controller
keeps an independent metadata copy before handing over the clone and uses that
copy with an isolated index and an environment without credentials. Clean/smudge
filters are disabled before checkout and remain disabled during extraction and
publication. This prevents a candidate from executing configured filters or
hiding edits by committing them locally. Only clones registered by the controller
can be inspected this way. Filter-dependent checkouts such as Git LFS are not
materialized by this lane; dedicated support requires separate validation.
The metadata copy is a prerequisite for future recovery, not an enabled automatic
conversation-resume mechanism. Existing diff gates and protected paths still apply.
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
  Quota -- ok --> Run[Restricted provider tools, no GitHub token]
  Run --> Gate{Controller gate: protected paths, diff caps}
  Gate -- refused --> Failed[agent:failed with reason]
  Gate -- passed --> PR[Draft PR, agent:review]
  PR --> CI[Fleet required checks] --> Merge[Abdel merges]
```

Before work, branch publication, draft creation and CI-repair publication, the
controller rechecks that the repository is public, owned by the configured owner,
active, and that the task is open, unchanged since this run began, and in the expected
authorized operational state. Proposal annotations do not grant authority or
conflict with that state. Multiple operational states are refused.
Missing verification or a failed lookup refuses the operation. After detecting
revocation it does not post cleanup comments or change labels. GitHub reads and
writes are separate requests; these checks do not provide an atomic transaction
against changes occurring between requests. A refusal after an authorized branch
push can leave that branch without a draft PR. Provider quota and repair history
are retained.

One-shot OpenCode API adapters use a constant session title to prevent the
client from requesting an extra model-generated title. This does not shorten
task reasoning, disable compaction or change the interactive chat interface.
It does not establish complete accounting for every possible internal SDK call.

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
The [30 October research review](docs/harness-research-2026-10-30.md) maps
newer evidence to controlled experiments and records the limits of our claims.

## Contributing

Changes go through pull requests with Conventional Commit titles. See the
[handbook](https://github.com/Abdel-Abdelaal/.github/tree/main/standards) and
[CONTRIBUTING](https://github.com/Abdel-Abdelaal/.github/blob/main/CONTRIBUTING.md).

## License

[MIT](LICENSE)
