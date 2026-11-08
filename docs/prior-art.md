# What we depend on and what we borrowed

This is the ledger of everything the controller relies on from outside this
repository: the software it runs, and the ideas taken from other projects. Keep
it current. A new feature gets a row here in the same PR that adds it.

## Rule: look for prior art before building

Before adding a feature or a fix, especially one a benchmark failure points to:

1. Search open-source agent harnesses, PR bots and papers for someone who has
    already solved it. Start with the projects below, then search more widely.
2. Prefer building on what exists: a maintained library, a feature of a tool we
    already run, or a design we can copy. Check its licence and that it is
    maintained. Code we copy keeps its notice; a new dependency is pinned and
    passes `deptry`.
3. Build our own only when nothing fits or what exists would weaken a gate. Say
    why in the PR.
4. Record the result here, including what was checked and not taken.

## Software we run

Python packages are pinned in `uv.lock`; this table lists the ones that shape
behaviour and the tools outside Python.

| What | Licence | Used for | Pinned by |
| --- | --- | --- | --- |
| [OpenCode](https://github.com/anomalyco/opencode) | MIT | worker, planner and reviewer runtime for every provider | version 1.18.35, installed on the host and recorded in [harness-comparison.md](harness-comparison.md) |
| [pydantic](https://github.com/pydantic/pydantic), [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | settings and record validation | `uv.lock` |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | GitHub and provider HTTP calls | `uv.lock` |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | cycle leases and process checks | `uv.lock` (exact version) |
| [uv Docker image](https://github.com/astral-sh/uv) | MIT or Apache-2.0 | sandbox base image | digest in `src/agent_dispatcher/sandbox.py` |
| [GitHub CLI](https://github.com/cli/cli) | MIT | the assistant's read-only GitHub tool | installed on the host |
| Dev tools: ruff, pyright, pytest, pytest-cov, import-linter, deptry, vulture | MIT, MIT, MIT, MIT, BSD-2-Clause, MIT, MIT | checks in CI and in the sandbox | `uv.lock` |

No upstream source code is copied into this repository.

## Ideas we took

"Checked first" says whether the source was found before we built the feature
(yes) or after (later, recorded retroactively).

| Feature | PR | Source | Licence | What we took | Checked first |
| --- | --- | --- | --- | --- | --- |
| OpenCode as the worker runtime | early | [OpenCode](https://github.com/anomalyco/opencode) | MIT | the whole runtime, as a pinned dependency | yes ([decision 0001](decisions/0001-build-vs-adopt.md)) |
| Sandboxed `run` tool: the worker executes tests and checks | sandbox work | [SWE-agent](https://github.com/SWE-agent/SWE-agent) agent-computer interface, [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | MIT | the idea of executable feedback; our tool and Docker sandbox are our own | yes ([research 30 Oct](harness-research-2026-10-30.md)) |
| Reproduce the bug with a failing test first | #88 | [Agentless](https://github.com/OpenAutoCoder/Agentless) reproduction tests, SWE-agent | MIT | the instruction and the reviewer rule; no code | yes |
| `/fix <what>` owner comments start a follow-up run | #97 | [ProPR](https://github.com/integry/propr) `/fix` command | Apache-2.0 | the comment-command idea; owner-only intake and gates are ours | yes ([research 8 Nov](harness-research-2026-11-08.md)) |
| Run summary table on each agent PR | #98 | ProPR run summaries | Apache-2.0 | the idea of a per-run summary; the fields are ours | yes |
| Keep a scanned excerpt of why a call failed | #95 | none found | n/a | our own | yes |
| Reviewer lists each stated requirement as met or not | #99 | [PR-Agent](https://github.com/The-PR-Agent/pr-agent) ticket compliance (`ticket_requirements`, `fully_compliant_requirements`, `not_compliant_requirements`) | MIT | same shape; ours differs in that the controller, not the model, turns any unmet requirement into a rejection | later |
| Read a JSON verdict that follows prose | #100 | PR-Agent's fallback YAML parsing of model replies; [json_repair](https://github.com/mangiucugna/json_repair) | MIT, MIT | from PR-Agent, the idea of fallbacks after the strict parse; json_repair not adopted (below) | later |
| Treat a reply cut at the output limit as unfinished | #86 | not checked | n/a | our own | no |
| Turn a capped planner's notes into a plan | #87 | not checked | n/a | our own | no |
| Keep edits at the hard turn cap; delete files the worker emptied | #91 | not checked | n/a | our own | no |
| State the diff caps in every coding prompt; one shrink call | #92 | not checked | n/a | our own | no |
| Retry a call that fails with no reason, once | #93 | common retry practice | n/a | our own | no |

## Checked and not taken

| Candidate | Licence | Why not |
| --- | --- | --- |
| [json_repair](https://github.com/mangiucugna/json_repair) for review verdicts | MIT | It repairs broken JSON by guessing. A guessed verdict could approve a change the reviewer never approved; verdicts must fail closed. Our fallback only reads an object that is already valid. |
| OpenCode structured output (schema-validated final answer with retries) | MIT | Exists in the OpenCode SDK and server API, not in `opencode run` 1.18.35, which the controller uses. Revisit if the worker moves to the server API; it would replace both #100's fallback and the format retry. |
| PR-Agent's third bucket, "requires further human verification" | MIT | A run has no human in the loop before the PR; an unverifiable requirement is better counted as unmet. Revisit if reviewers start marking verifiable ones unmet. |
| OpenHands, Goose, LangGraph, Letta, elizaOS as frameworks | MIT / Apache-2.0 | See [research 8 Nov](harness-research-2026-11-08.md): no score gain for the code we would have to keep safe. |
| Language-server diagnostics on every edit (OpenCode LSP, Aider lint-on-edit) | MIT / Apache-2.0 | Servers would run on the host outside the sandbox; the worker already runs ruff and pyright in it ([research 6 Nov](harness-research-2026-11-06.md)). |

## Open candidates

Each waits on evidence from a benchmark trace or on Abdel's decision.

| Candidate | Source | Waiting on |
| --- | --- | --- |
| Best of N patches on hard tasks, picked by our checks | [Agentless](https://github.com/OpenAutoCoder/Agentless), [SWE-Replay](https://arxiv.org/pdf/2601.22129) | Abdel's go-ahead on token spend (roughly doubles hard tasks) |
| Context condensing for long calls | [OpenHands](https://github.com/OpenHands/software-agent-sdk) condenser | traces showing continued calls failing for lost context |
| Repository symbol map in the prompt | [Aider repo map](https://github.com/Aider-AI/aider) (Apache-2.0) | traces showing turns spent finding code |
| Stop repeated identical tool calls | OpenCode's repeated-call guard | a test that our deny-by-default config already covers it |
| Semgrep rules as an extra sandbox check | [Semgrep registry](https://semgrep.dev/docs/running-rules) (Semgrep Rules License) | a benchmark miss a rule would have caught |
