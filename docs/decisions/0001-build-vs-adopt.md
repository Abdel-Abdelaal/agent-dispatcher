---
status: accepted
date: 2026-10-25
---

# Build the controller core, adopt only OpenCode as a harness

## Context and problem statement

The [design report](../design.md) recommends a thin controller of our own, and
left two checks open before any open-source tool replaces part of it:

- Item 12: OpenCode server mode, aider, Goose, LiteLLM, OpenHands,
  claude-code-router, OpenSpec, Serena and Context7 were not researched.
- Item 13: a Cyrus trial, to see whether it supports owner-only triggers and
  pre- and post-run hooks around the official CLIs.

We want to build on open source where it fits and credit it, while keeping our
code independent. A tool can only replace a piece of `agent-dispatcher` (intake,
routing, sandbox, gate or review) if it keeps the rules in `AGENTS.md`:

1. Only labels the owner sets start a task.
2. Claude and ChatGPT subscription logins are used only through the official
   `claude -p` and `codex exec` clients.
3. Agent runs never receive a GitHub token. Only the controller publishes.
4. The controller, not the agent, decides whether a diff may become a PR.
5. It fits a VPS with about 2 GiB of free RAM, runs headless and sends no
   telemetry we haven't turned on.

Evidence was gathered on 2026-10-25 from each project's source, docs, GitHub
releases and package registry. Cyrus was read at commit
[`c48d06c`](https://github.com/cyrusagents/cyrus/tree/c48d06c15bb16a913edc510e3e66848a50d78630).
Nothing was installed on the VPS or run against a repository.

## Considered options

| Tool | Licence | Latest release | Headless | Subscription login | Could help with | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Cyrus | Apache-2.0 | v0.2.73, 2026-09-30 | yes, daemon | Claude via Agent SDK and `setup-token` | intake, sandbox | reject |
| OpenCode | MIT | v1.19.4, 2026-10-22 | yes, `run` and `serve` | ChatGPT Plus built in; Claude plugins unbundled | MiMo and NVIDIA runs, review | adopt `run`; reject `serve` |
| aider | Apache-2.0 | 0.86.2 on PyPI, 2026-02-12 | yes, `--message` | no, API keys | MiMo and NVIDIA runs | reject |
| Goose | Apache-2.0 | v1.56.0, 2026-10-23 | yes, `goose run` | yes, through third-party ACP adapters | MiMo and NVIDIA runs | reject |
| LiteLLM | MIT core, commercial `enterprise/` | v1.106.4, 2026-10-25 | yes, proxy | no | routing | reject |
| OpenHands | MIT | v1.33.0, 2026-10-23 | yes, Agent Server | can drive Claude Code and Codex | sandbox, runs | reject |
| claude-code-router | MIT | v3.1.3, 2026-10-09 | yes, npm CLI or Docker | proxies Claude Code and Codex, imports logins | routing | reject |
| OpenSpec | MIT | v1.15.0, 2026-10-24 | yes, CLI | no | intake, spec checks | trial |
| Serena | GPL-3.0-or-later app, MIT `solidlsp` | v2.1.0, 2026-10-19 | yes, MCP server | no | runs | reject for now |
| Context7 | MIT client, private backend | ctx7 0.8.2, 2026-10-23 | yes, MCP or CLI | no, Context7 API key | runs | reject |

All ten repositories had commits in October 2026 except aider (last push
2026-05-22) and none is archived.

### Cyrus

Cyrus is the closest match in shape: a daemon that turns tracker events into
agent sessions in per-issue git worktrees. It fails three of the five rules by
design, not by configuration.

- **Owner-only label triggers: no.** The GitHub transport accepts only
  `issue_comment`, `pull_request_review_comment`, `pull_request_review` and
  `push` events and ignores everything else, so `issues.labeled` never arrives
  ([GitHubEventTransport.ts](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/github-event-transport/src/GitHubEventTransport.ts#L242-L251)).
  Comments on plain issues are dropped ("Ignoring GitHub comment on non-PR
  issue"); GitHub work starts from an `@mention` on a pull request, and the only
  author check is that the commenter isn't the bot itself
  ([EdgeWorker.ts](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/edge-worker/src/EdgeWorker.ts#L1399-L1455)).
  Its user allowlist applies to Linear agent sessions
  ([EdgeWorker.ts](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/edge-worker/src/EdgeWorker.ts#L7050-L7062)).
  Issue intake is Linear's.
- **Pre- and post-run hooks around `claude -p` and `codex exec`: partly.**
  `cyrus-setup.sh` runs when a worktree is created and `cyrus-teardown.sh` runs
  when the issue reaches a terminal state, with a two-minute timeout
  ([SETUP_SCRIPTS.md](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/docs/SETUP_SCRIPTS.md)).
  Both are read from the issue worktree, so they are repository content, and
  teardown is cleanup, not a gate that can stop a publish. Cyrus doesn't run the
  CLIs either: it embeds `@anthropic-ai/claude-agent-sdk` and `@openai/codex-sdk`
  ([claude-runner](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/claude-runner/package.json),
  [codex-runner](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/codex-runner/package.json)),
  and its self-hosting guide feeds the Agent SDK a Max subscription token from
  `claude setup-token`. That is a less clearly supported use of a subscription
  than the official CLI we call.
- **No GitHub token in the agent process: no.** The agent opens PRs itself with
  the host's `gh` login, and "Cyrus operates with the same permissions as your
  authenticated Git and GitHub CLI user"
  ([GIT_GITHUB.md](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/docs/GIT_GITHUB.md)).
  When a GitHub App token is stored for the repository, it is put in the session
  as `CYRUS_GH_TOKEN`
  ([RunnerConfigBuilder.ts](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/packages/edge-worker/src/RunnerConfigBuilder.ts#L184-L195)).
- **Licence, telemetry and maintenance.** Apache-2.0. Error reporting goes to
  the vendor's Sentry project by default through a DSN compiled into release
  builds; `CYRUS_SENTRY_DISABLED=1` turns it off
  ([createErrorReporter.ts](https://github.com/cyrusagents/cyrus/blob/c48d06c15bb16a913edc510e3e66848a50d78630/apps/cli/src/services/createErrorReporter.ts#L17-L60)).
  It is actively maintained: v0.2.73 on 2026-09-30, commits on 2026-10-25,
  871 stars and 120 open issues. A paid cloud tier (atcyrus.com) sits beside
  the community self-hosted mode.

Item 13 asked for a trial on `shortlink`. The source review answers its two
questions with "no" and "partly", and a trial run can't change behaviour that
the code doesn't have, so no trial was run.

### OpenCode (server mode)

The fleet already runs OpenCode with MiMo Flash as its advisory reviewer, and
this repository commits an `opencode.json` with every permission denied by
default. `opencode run` runs one prompt headless, and `opencode serve` exposes
an HTTP API with optional basic auth
([server docs](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/server.mdx)).
Its docs say Anthropic prohibits using Claude Pro/Max through OpenCode
plugins, which stopped being bundled in 1.3.0; ChatGPT Plus login is built in
([provider docs](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/providers.mdx)).
We use it only with API-key providers (MiMo and NVIDIA) and never connect a
Claude or ChatGPT login to it; Codex runs stay on `codex exec`. Server mode is
rejected: runs are serial and one-shot, and a long-lived HTTP server adds an
attack surface that `opencode run` doesn't need.

### aider

Apache-2.0, headless through `--message` and `--yes-always`, API keys only, so
there is no terms risk ([aider](https://github.com/Aider-AI/aider)). The latest
GitHub release is v0.86.0 from 2025-08-09, the latest PyPI release 0.86.2 from
2026-02-12, and the last push 2026-05-22: activity has slowed. It would do the
same job as OpenCode, which the fleet already configures and pins.

### Goose

Apache-2.0, now under the Linux Foundation's Agentic AI Foundation
([Goose](https://github.com/aaif-goose/goose)). Usage data is opt-in on first
run ([usage data](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/usage-data.md)).
Its recommended way to use a Claude or ChatGPT subscription is through ACP
adapters such as `claude-agent-acp` and `codex-acp`, which are third-party
clients wrapping the subscription
([ACP providers](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/acp-providers.md)).
**Terms risk:** only official clients may use subscription logins. With API
keys only, it overlaps OpenCode.

### LiteLLM

The core is MIT, but features under `enterprise/` need the LiteLLM Commercial
License ([LiteLLM](https://github.com/BerriAI/litellm)). On 2026-10-21 a
malicious `litellm` 1.107.0 with a credential stealer was published to PyPI and
Docker and then removed
([BerriAI/litellm#47826](https://github.com/BerriAI/litellm/issues/47826)), and
the proxy has had two critical advisories since May 2026 (authentication bypass
and privilege escalation to proxy admin;
[advisories](https://github.com/BerriAI/litellm/security/advisories)). There
is also an open request for a way to stop the proxy calling remote services
([BerriAI/litellm#46466](https://github.com/BerriAI/litellm/issues/46466)).
MiMo and NVIDIA both speak the OpenAI API already, and routing in our design
means picking a provider from quota, which the quota guard does. A gateway
holding every provider key would be the most valuable target on the VPS.

### OpenHands

MIT, very active, and headless through its Agent Server REST API
([OpenHands](https://github.com/OpenHands/OpenHands)). It is built around
Docker sandboxes and running several agents at once, and can drive Claude
Code and Codex as third-party agents. The VPS has about 2 GiB of free RAM, and
we run one CLI at a time, so it brings more than we can host or need.

### claude-code-router

MIT, maintained, and now a desktop-first "control plane" with an npm CLI and
Docker mode ([claude-code-router](https://github.com/musistudio/claude-code-router)).
It proxies Claude Code and Codex traffic to other providers and lists "local
login import", "Chrome login-state import" and credential pools with key
rotation. **Terms risk:** sending subscription OAuth traffic through a proxy,
or pooling accounts, is outside what Anthropic and OpenAI allow. We don't need
Claude Code to talk to other models; MiMo and NVIDIA runs go through OpenCode.

### OpenSpec

MIT, active, headless CLI that writes change proposals and specs as Markdown in
the repository ([OpenSpec](https://github.com/Fission-AI/OpenSpec)). Anonymous
usage telemetry is on unless `OPENSPEC_TELEMETRY=0` or `DO_NOT_TRACK=1` is set
(it is off in CI). It could give intake and the MiMo spec check a fixed format
for "what this task must change", which a gate can compare the diff against.
Worth a trial, not adoption, because it adds files to every target repository.

### Serena

Semantic code navigation and editing through language servers, served as an MCP
server ([Serena](https://github.com/oraios/serena)). The application is
GPL-3.0-or-later and only its `solidlsp` package is MIT. It would run inside
the agent sandbox as a separate process, so the GPL doesn't reach our code, but
it adds language servers and a web dashboard to a 2 GiB host. Claude Code and
Codex already navigate small repositories well. Revisit if runs fail on code
navigation in larger repositories.

### Context7

The MCP server and `ctx7` CLI are MIT, but the "API backend, parsing engine,
and crawling engine" are private, and calls go to `mcp.context7.com` with an
API key ([Context7](https://github.com/upstash/context7)). Using it would put a
key and an outbound network path into the sandbox, the combination our
prompt-injection defence removes.

## Decision outcome

Chosen option: build the controller core ourselves (intake, queue, routing,
quota, gate and publishing) and adopt only OpenCode, as the harness for MiMo
and NVIDIA implementation runs through `opencode run` with API-key providers.
No tool reviewed keeps owner-only intake, subscription use through official
clients only, a credential-free agent process and a controller-side gate
together.

| Tool | Verdict | Revisit when |
| --- | --- | --- |
| Cyrus | reject; design idea credited | it supports `issues.labeled` with an author allowlist and publishes outside the agent process |
| OpenCode `run` | adopt for MiMo and NVIDIA runs | never with a Claude or ChatGPT login |
| OpenCode `serve` | reject | runs become long-lived or parallel |
| aider | reject | OpenCode stops fitting |
| Goose | reject | never for subscription use; otherwise only if OpenCode stops fitting |
| LiteLLM | reject | not planned |
| OpenHands | reject | the platform moves to a host with room for Docker sandboxes |
| claude-code-router | reject | not planned |
| OpenSpec | trial in Phase 2 on `shortlink`, telemetry off | the trial result is recorded in a new ADR |
| Serena | reject for now | runs fail on code navigation in larger repositories |
| Context7 | reject | not planned |

### Consequences

- Good: our code stays small and independent, every rule in `AGENTS.md` holds,
  and no subscription traffic goes through a third-party client.
- Good: OpenCode is already configured, pinned and reviewed in the fleet, so
  MiMo and NVIDIA runs reuse a known setup.
- Bad: we keep writing and maintaining intake, worktree handling and
  publishing that Cyrus already has.
- Bad: OpenCode updates fast (v1.19.4 on 2026-10-22), so its version needs the
  same pinning and 14-day cooldown as other dependencies, with `autoupdate` and
  `share` left off.
- Neutral: Cyrus, Symphony, gh-aw and Codeman stay credited in the README as
  design sources; none of their code is used.
