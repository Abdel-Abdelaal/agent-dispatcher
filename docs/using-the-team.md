# Using the agent team

The controller is already installed on the laptop. The Windows scheduled task
`agent-dispatcher` polls every 30 minutes while the laptop is awake, online and
the owner is signed in. Use the scheduled task for live runs: it also performs
trusted audit intake and sends scrubbed telemetry. The bare Python command does
not perform those host integration steps.

## Give it work

Tell the assistant the repository, one small outcome and how to verify it:

> Queue shortlink: reject duplicate aliases. Done when a regression test covers
> two requests for the same alias and the documented response matches the API.

The assistant can create the issue and add `agent:ready` as the owner. You can
also create an issue yourself in the target repository and add that label.
An existing Task issue form provides goal and acceptance-criteria fields.
Do not put credentials or confidential information in public issues.

Use this task body:

```text
Goal: one observable change.
Scope: files or component, plus anything that must stay unchanged.
Done when: specific behavior and its regression test or manual check.
Verification: relevant existing test command, or steps and expected result.
```

The controller refreshes public, owned, non-fork, non-archived repositories each
cycle. A new repository needs its normal workflows and agent labels enrolled.
Private repositories and the controller itself are excluded from autonomous
agent work. Existing private CI is separate.

Weekly audits create `agent:proposed` suggestions. The trusted host can promote
an eligible suggestion automatically after verifying the signed audit receipt
and unchanged bot issue. Autonomous intake is capped at one new task per UTC day
on this host. Manually queued work still runs serially and respects provider
availability. A signed suggestion can be wrong; the agent may decline it.

## Start, inspect or pause

On Abdel's configured laptop, run `mimo` and describe what you want normally:

> I'm not sure which repo needs attention. Look at my projects and suggest a task.

> Fix duplicate aliases in shortlink, add a regression test and open a draft PR.

> Create a private repo called garden-notes for a gardening project.

The conversational assistant can discover public projects, inspect files, issues
and PR checks, search GitHub projects, create an owned repo and queue code work.
GitHub authentication stays in controller tools, never in model messages. GitHub
writes show the exact action and require a terminal `y`. Repo creation defaults
to private. Autonomous private coding is not enabled; new public repos still need
fleet enrollment before their workflows can be assumed present.

The chat loads [shared project context](assistant-context.md) and any notes you
explicitly ask it to remember and approve. It does not inherit other assistants'
chat histories or every tool they have. Conversation history is in memory only;
restarting retains the shared brief and approved local notes. Chat telemetry is
hashes/sizes/outcomes/known usage, kept locally in `runs/assistant-telemetry`, not
yet included in public dashboard ingestion. No raw chat transcript is retained.

Use `/status`, `/reset` and `/quit` in chat. Shell commands `mimo status` and
`mimo logs` remain available without a model call. `mimo start` requests a cycle
for already queued work and saves MiMo-only planning, coding and review for future
cycles. `mimo task` retains the optional explicit task form. The scheduler handles
audit intake and telemetry delivery; queued issues run serially, not necessarily
immediately. Active or paused runs are not overridden. No failed task is reopened
automatically. MiMo quota still applies, with no paid fallback or automatic retries.

The chat allows at most six model exchanges per user turn, four tool calls per
exchange, a context character budget,90-second model request timeouts and45-second
controller command timeouts. A five-minute elapsed guard is checked between calls;
it is not an exact whole-process deadline and waits for terminal approval are not
bounded. It does not fix the dispatcher's separate durable-deadline limitation.
There is no arbitrary shell, deployment or secret-management tool. A tool failure
is evidence of a failed operation, never approval to bypass policy or buy capacity.

The existing installed shortcut points to `scripts/mimo.ps1` in protected main.
The chat reuses the configured MiMo API adapter, structured subprocess runner,
typed tool validation and existing gated coding pipeline, with no new dependency.
Its tool protocol follows [MiMo's API documentation](https://mimo.mi.com/docs/api/text-generation/openai-api).

These PowerShell commands use the installed controller. They do not print keys.

```powershell
# Request one live cycle; the scheduler ignores an overlapping instance.
schtasks /Run /TN agent-dispatcher

# Show queued work, recent runs and parked providers without a model call.
Set-Location D:\showcase\agent-dispatcher
.\.venv\Scripts\agent-dispatcher.exe status

# Pause or resume future scheduled runs.
schtasks /Change /TN agent-dispatcher /DISABLE
schtasks /Change /TN agent-dispatcher /ENABLE
```

Disabling the schedule does not cancel an already running process. Avoid manual
parallel invocations. A direct `once --dry-run` reads GitHub and can update the
local queue, but does not call a model or publish a PR; polling requires the
controller's existing GitHub authentication.

The local run log is `runs/dispatcher.log`. GitHub issue labels show progress:
`agent:running`, `agent:review` (draft PR), `agent:parked` or `agent:failed`.
Read the issue comment for the reason when work fails or is refused.

## Review the result

The team opens a draft PR. Check its actual diff against the acceptance criteria,
the regression test, the latest required CI results and unresolved review
findings before merging. Passing CI only establishes the checks that ran.
AI approval and a telemetry signature do not establish correctness.

The controller can attempt one repair of failed required CI when no task is
waiting. A second failure needs investigation rather than an unlimited retry
loop. Do not weaken tests or protections to get a green result.

An invalid review verdict gets one formatting retry by the read-only reviewer.
It never authorizes a code edit or counts as approval. Two invalid replies stop
the task without publishing a PR.

The fleet shares reusable checks, with additional checks appropriate to each
repository. Shortlink's application checks and PR-Agent trial are not universal.
Old open PRs need fresh checks on their latest commit; new repo enrollment must
finish before its checks can be assumed present.

## When providers run out

The deployed host disables the Claude subscription provider. Codex plans and
reviews when available; MiMo implements. If the thinkers are unavailable, the
owner-enabled fallback uses separate MiMo planning, implementation and read-only
review calls. Same-provider review has less independence. All gates remain.
When every configured provider is exhausted, tasks wait for reset. No fallback
buys capacity or enables a paid service.

Exact account-wide remaining usage is not available to the headless quota
reader. Exhaustion messages park providers; the configured share is not proof of
an account-wide spending cap. The host also limits new autonomous work and model
turns. Keep service-side free-only spending controls in place.

## Evidence and limits

The [public dashboard](https://abdel-abdelaal.github.io/github-governance/)
shows verified records and measured usage where available. Agent signatures
attest hosted ingestion of controller reports, not independent local execution.
Telemetry retains hashes, sizes and bounded scrubbed excerpts. Unknown usage
stays unknown; a record is not necessarily a successful task.

Workers currently read, search and edit files. The controller handles GitHub and
CI runs tests after publication. Workers have no general shell, browser, deploy
or arbitrary MCP access. Local execution is not an operating-system sandbox.
Changes needing browser verification, migrations, deployments or protected
configuration require a separately reviewed execution path. Do not grant broad
tools or credentials merely to avoid a refusal.

This system reduces repeated work and records evidence. Defect-free development,
universal tool coverage and superiority over a simpler workflow are not proven.
