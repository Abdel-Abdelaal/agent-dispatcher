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
| Two candidate changes on hard tasks; the controller keeps one | #102 | [Agentless](https://github.com/OpenAutoCoder/Agentless) patch selection (regression and reproduction tests, then majority vote over normalized patches), the reproduction-test selection of [CodeMonkeys](https://arxiv.org/abs/2501.14723), sampling results from [SWE-Replay](https://arxiv.org/pdf/2601.22129) and [SWE-HERO](https://arxiv.org/pdf/2604.01496) | MIT (Agentless) | the design: filter by the repository's checks, then run each candidate's own tests against the other; no code. With two candidates a vote decides nothing, so size breaks ties | yes |
| Start the second candidate whenever a shorter call still fits, with its calls ending early | #103 | AI21's [budget-aware best-of-N](https://www.ai21.com/?p=30517) (extra rollouts launched to fit the budget), [Budget-Aware Tool Use](https://arxiv.org/pdf/2511.17006) | n/a (articles) | the idea that the number of samples follows the budget left rather than a fixed reserve; AI21's confidence classifiers not taken (a model would decide) | yes |
| Run the original versions of edited existing tests against the change; tell the reviewer and rank candidates by them | #104 | [Agentless](https://github.com/OpenAutoCoder/Agentless) regression tests (patches that break existing passing tests are filtered out), SWE-bench PASS_TO_PASS tests, [Silent Regressions: The Verification Gap in Coding Agents](https://neurips.cc/virtual/2026/171452) | MIT (Agentless) | the regression idea, applied to the tests a change rewrote; the reviewer judges whether the task asks for each behaviour change, since real fixes do change assertions; no code | yes |
| A held-out benchmark set no harness change is made from | #110 | [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) and [SWE-rebench](https://swe-rebench.com/) (fresh tasks to avoid contamination), the usual development and test split | MIT (SWE-bench-Live) | the idea of tasks the system was never fitted to, graded like SWE-bench; the tasks are our own fleet fixes | yes |
| Treat a reply cut at the output limit as unfinished | #86 | not checked | n/a | our own | no |
| Turn a capped planner's notes into a plan | #87 | not checked | n/a | our own | no |
| Keep edits at the hard turn cap; delete files the worker emptied | #91 | not checked | n/a | our own | no |
| State the diff caps in every coding prompt; one shrink call | #92 | not checked | n/a | our own | no |
| Retry a call that fails with no reason, once | #93 | common retry practice | n/a | our own | no |
| Retry a call up to three more times when the provider streams a tool call without an id | #109 | [anomalyco/opencode#6290](https://github.com/anomalyco/opencode/issues/6290) and the closed, unmerged fix [#12585](https://github.com/anomalyco/opencode/pull/12585) (generate a fallback id); the OpenAI and AI SDK clients' bounded retries of transient errors | MIT | the diagnosis, and bounded retries; the fallback id is a patch to OpenCode's bundled AI SDK, which we pin and do not patch | yes |
| Work on several benchmark tasks at once (`--parallel`) | #117 | [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)'s `--workers` and the [SWE-bench harness](https://github.com/SWE-bench/SWE-bench)'s `--max_workers`: a thread pool over instances, results collected as they complete | MIT | a thread pool with `as_completed` and a default of one at a time, as in mini-swe-agent; mini-swe-agent locks its shared predictions file, and here only the main thread prints; our own: a lock per mirror, a telemetry folder per run, and no new task starting after one raises | yes |
| Count how often the team's "done" is wrong (`wrong_done`, `done_precision`) | #118 | Precision, as for any classifier: published is the claim, the hidden tests are the truth | n/a | the measure only; SWE-bench reports resolved alone | yes |
| Acceptance tests written from the issue alone before the change, kept only if they fail on the unchanged code, then run on each candidate, shown to the reviewer, used to choose and reported in the PR (off by default since #121: 29% more tokens, no wrong change caught, 3 false flags in 25 tasks) | #119 | [Otter / e-Otter++](https://arxiv.org/abs/2508.06365) (tests from issues, improved with execution feedback), [SWT-bench](https://arxiv.org/abs/2406.12952) (fail-to-pass rule; generated tests as a filter double SWE-agent's precision), [Issue2Test](https://arxiv.org/abs/2503.16320) | papers; no code | the fail-to-pass rule, tests from the issue alone with the run tool for feedback, and use as a filter and ranking signal rather than a hard gate, since generated tests are often wrong; our own: one worker round to meet them, the reviewer told, the PR table | yes |
| A sandbox fault while grading leaves that task ungraded instead of stopping the benchmark | #120 | The [SWE-bench harness](https://github.com/SWE-bench/SWE-bench) records an instance whose evaluation errors as an error instance in its report and goes on with the rest | MIT | reporting errors per task, apart from resolved and unresolved; our own: three tries 30 seconds apart first, since our faults were brief ssh or docker failures | yes |

## Checked and not taken

| Candidate | Licence | Why not |
| --- | --- | --- |
| [json_repair](https://github.com/mangiucugna/json_repair) for review verdicts | MIT | It repairs broken JSON by guessing. A guessed verdict could approve a change the reviewer never approved; verdicts must fail closed. Our fallback only reads an object that is already valid. |
| OpenCode structured output (schema-validated final answer with retries) | MIT | Exists in the OpenCode SDK and server API, not in `opencode run` 1.18.35, which the controller uses. Revisit if the worker moves to the server API; it would replace both #100's fallback and the format retry. |
| PR-Agent's third bucket, "requires further human verification" | MIT | A run has no human in the loop before the PR; an unverifiable requirement is better counted as unmet. Revisit if reviewers start marking verifiable ones unmet. |
| OpenHands, Goose, LangGraph, Letta, elizaOS as frameworks | MIT / Apache-2.0 | See [research 8 Nov](harness-research-2026-11-08.md): no score gain for the code we would have to keep safe. |
| A trained critic model to pick the candidate ([OpenHands critic](https://openhands.dev/blog/sota-on-swe-bench-verified-with-inference-time-scaling-and-critic-model), [critic rubrics](https://arxiv.org/pdf/2603.03800)) | MIT (code); model weights on Hugging Face | Needs a GPU-served model we don't run, and its scores come from other models' trajectories. Our tests-first choice is deterministic. Revisit if two candidates often tie on every test signal. |
| Language-server diagnostics on every edit (OpenCode LSP, Aider lint-on-edit) | MIT / Apache-2.0 | Servers would run on the host outside the sandbox; the worker already runs ruff and pyright in it ([research 6 Nov](harness-research-2026-11-06.md)). |

## Open candidates

Each waits on evidence from a benchmark trace or on Abdel's decision.

| Candidate | Source | Waiting on |
| --- | --- | --- |
| More than two candidates, or candidates below hard | Agentless (up to 40 samples), SWE-Replay (10) | a benchmark showing two candidates help, and the token cost per extra one |
| Context condensing for long calls | [OpenHands](https://github.com/OpenHands/software-agent-sdk) condenser | traces showing continued calls failing for lost context |
| Repository symbol map in the prompt | [Aider repo map](https://github.com/Aider-AI/aider) (Apache-2.0) | traces showing turns spent finding code |
| Stop repeated identical tool calls | OpenCode's repeated-call guard | a test that our deny-by-default config already covers it |
| Semgrep rules as an extra sandbox check | [Semgrep registry](https://semgrep.dev/docs/running-rules) (Semgrep Rules License) | a benchmark miss a rule would have caught |
