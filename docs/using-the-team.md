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
>
> Fix duplicate aliases in shortlink, add a regression test and open a draft PR.
>
> Create a private repo called garden-notes for a gardening project.

`mimo` opens the real, pinned OpenCode1.18.35 terminal interface with the
`trifecta` primary agent and MiMo v2.6 Flash. The custom Python chat loop has been
removed. MiMo is the only enabled provider in this UI. No CLI upgrade, new service,
paid fallback or new project dependency is introduced.

The connected `trifecta_*` MCP tools discover public projects, inspect files,
issues and PR checks, search GitHub projects, create an owned repo and queue code
work. OpenCode's native permission UI asks before `create_repo`, `queue_task`,
`start` and `remember`. The controller still verifies owner identity and repository
policy. No GitHub token is placed in model messages or provider configuration.
The MCP transport is local stdio, not a network service. A read-only connection
refuses writes even if a client requests one. Completed and failed write requests
are deduplicated during the bridge process lifetime; this is not cross-restart
idempotency. Do not blindly retry a write after an ambiguous network error.

Long coding work is real background work: the assistant queues an issue and starts
the existing scheduler, then uses status and PR checks to report actual progress.
The separate planner, worker and reviewer retain the same gates. With MiMo-only
fallback, those roles use separate MiMo calls, not three independent providers.
If the planner rates a task harder but its model and effort stay the same, the
controller keeps that completed plan instead of spending another call repeating
it. A stronger-model refinement receives the previous plan. Empty plans, refusals
and malformed triage decisions stop before coding; an unavailable optional triage
service retains the existing skip behavior. These checks do not guarantee quality.
Repo creation defaults to private. Private autonomous coding remains unavailable;
new public repos still need fleet enrollment/checks before coverage is assumed.

The native UI loads [shared project context](assistant-context.md) and approved
notes. It does not inherit other assistants' full histories or every tool. Native
sessions, prompts, tool outputs and usage persist locally under
`runs/native-opencode`. The launcher resumes the latest valid session by ID;
an empty profile opens the normal home screen without attempting a resume.
Sharing is disabled. These confidential native session records are not published
or currently ingested into the public dashboard. The controller bridge also keeps
hashes/outcomes locally in `runs/assistant-telemetry`. This replaces the old
in-memory-only chat behavior; do not publish the native session directory.

OpenCode supplies its normal conversation, session and permission interface.
Shell commands `mimo status` and `mimo logs` still work without a model call.
`mimo start` starts already queued work and saves MiMo-only mode for future cycles.
`mimo task` keeps the optional explicit form. Closing the UI does not stop an
already-started background dispatcher run. Requests run serially and may wait.

The front-end agent has no arbitrary shell, local file editing, deployment or
secret-management permission. Coding is delegated to isolated worker checkouts
rather than performed inside the controller's source tree. These are tool gates,
not an OS sandbox. Native agent steps are capped at20 per turn. OpenCode handles
its own context compaction and model retry behavior. The team's
[cycle supervisor](cycle-deadlines.md) separately bounds its child and recovers
expired dead leases. Neither guarantees long-task success.

The launcher isolates config, plugins and history from other apps, excludes
project configuration and other providers, disables updates/sharing, and uses
explicit agent and global tool permissions. It never passes `--auto`. Approval
is enforced by the reviewed native host; running the bridge independently with
its native-write flag is outside that trusted host path. Owner choices such as
remembered native approvals remain under the owner's control.

## Native integration references

This integration reuses the existing controller broker and worker pipeline. It
adds a small stdio MCP bridge, not another chat/model loop. OpenCode remains the
already pinned MIT-licensed upstream CLI; no upstream implementation was copied.
The exact1.18.35 MCP naming and permission/configuration schema were inspected.

- [OpenCode MCP integration](https://opencode.ai/docs/mcp-servers/)
- [OpenCode permissions](https://opencode.ai/docs/permissions/)
- [Pinned MCP client source](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/mcp/index.ts)
- [Pinned tool naming](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/mcp/catalog.ts)

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

Native OpenCode diagnostic state remains local and confidential. Ask the native
assistant to inspect a run by repository and run number; see the
[run evidence guide](native-run-evidence.md). Raw state is not a public export
and does not yet provide validated automatic conversation resume.

For candidate review, the native assistant can discover open PRs, read exact
commit diffs/files and task intent, identify required checks, and fetch the
matching Actions error lines. See [PR review tools](pr-review-tools.md).
