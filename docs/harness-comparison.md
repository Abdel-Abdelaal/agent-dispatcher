# MiMo harness comparison

The default remains `claude-code`. An explicit opt-in setting,
`DISPATCHER_MIMO_HARNESS=opencode`, selects an alternative adapter for the same
MiMo model. `DISPATCHER_OPENCODE_EXECUTABLE` selects the reviewed executable.
The fleet already pins OpenCode1.18.35; this change adds no project dependency or
CLI upgrade. CLI runtime bootstrap remains scoped to its temporary directories.

The controller's intake, protected paths, diff caps, review and draft-only output
remain unchanged. The alternative gets file tools only, no shell, outside-directory
access, web, subagents or MCP. Project/user configuration is excluded. Agent
steps use the existing per-level turn budget. OpenCode's native steps limit asks
for a final text response; it does not establish that code is correct or complete.
The shared20-minute process-tree timeout remains.

The pinned version's home-directory override also excludes user `.opencode`
agents/plugins, which project-config disabling alone does not exclude. This
version-specific behavior must be checked again before a CLI upgrade. The diff
gate now protects `.opencode` files from agent edits as well.

Prompts are attached as files outside the model worktree, never command arguments.
Configuration, logs, data, sessions and temporary files use a per-call temporary
directory that is removed afterward. This comparison does not enable durable raw
transcript retention. A future resume feature needs a separate confidential
storage design. Tool permissions do not constitute an operating-system sandbox.

The adapter extracts the final text event, explicit token counters and error
events. Unknown usage stays unknown. The existing pre-retention scanner and
hosted signing pipeline continue to handle telemetry. Native step limits and
wall-clock timeout bound each invocation; no paid capacity is provisioned.

## Comparison protocol

1. Keep the model, issue body, base commit and independent acceptance tests fixed.
2. Use fresh clones and record controller revision and harness for each attempt.
3. Measure behavioral checks, lint/types, required CI, review outcomes, tool-budget
   exhaustion, wall time and known usage. Do not infer quality from a successful
   CLI exit or an agent's approval.
4. Compare against the current harness before changing the deployed default.
   Do not switch a running task's configuration. Failed attempts remain evidence.

The first comparison can reuse the frozen nonstandard-HTTP-status task and its
held-out checks. Refactor evaluation separately verifies reuse, custom collision
semantics, clock reads and bounded retries without recursive stack growth.

## Sources and credits

This adapter reuses this project's existing OpenCode configuration and provider
result normalization. No upstream implementation is copied.

- [OpenCode](https://github.com/anomalyco/opencode) supplies the existing CLI;
  its [CLI docs](https://opencode.ai/docs/cli/) describe session control and JSON
  events. The pinned1.18.35 schema was inspected for steps, project-config flags
  and XDG paths rather than assuming the latest V2 configuration applies.
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  is a design reference for checkpointing and safe recovery.
- [OpenHands persistence](https://docs.openhands.dev/sdk/guides/convo-persistence)
  is a design reference for persistent conversations and execution boundaries.

The latter two frameworks are not installed by this change. Verify exact licenses
and required notices before any future code reuse. Existing CLI attribution and
dependency license obligations remain in place.
