# Harness research and experiment plan — 30 October 2026

We have a working controller, useful safeguards and measured failures. We do not
have evidence for general parity with frontier coding agents or a revolutionary
new method. The next contribution should be demonstrated through reproducible
completion, correctness and recovery results. This report separates literature,
our observations and proposed engineering. It supplements the earlier
[adoption decision](decisions/0001-build-vs-adopt.md).

## What our evidence actually establishes

| Observation | Evidence | Limit |
| --- | --- | --- |
| Agent-controlled Git config could execute a filter; a local commit could hide edits | Two regressions failed before [PR42](https://github.com/Abdel-Abdelaal/agent-dispatcher/pull/42); final suite 351 passed, 99.67% coverage; installed regressions passed | Closes these Git boundaries, not an OS sandbox or a model-quality benchmark |
| Completed conversation and reasoning can survive a session fork | Local synthetic controls preserve prior messages and the source message/part hash | No real-provider compatibility or interrupted-response guarantee |
| Reasoning in an unfinished stream is lost on forced client termination | Completed-stream control retains it; the matched interrupted fixture does not | Tested pinned OpenCode 1.18.35 and this fault mode; not every kind of interruption |
| The installed client has a separate output ceiling | Local request matrix: 32768 declaration gives 32000; 131072 declaration alone still gives 32000; declaration plus global override gives 131072, thinking enabled | Request shaping only, not provider acceptance or improved correctness |
| Time and token failures are different | PR64 advisory finished with length and no answer; PR42 advisory timed out three times after 600 seconds without an answer | Neither is an approval; missing usage remains unknown |

[Protocol summaries and hashes](evaluation-2026-10-30/manifest.json) include the
initial evaluator mistake about parent-session metadata. A corrected test checks
actual content preservation. Failed fixtures remain visible. No live model was
called for these local protocol experiments. Raw real reasoning stays private.

Xiaomi's [model specification](https://mimo.mi.com/models/en-US/mimo-v2.6-flash)
advertises 1M context and 128K output. Our current native helper uses operational
budgets of 196608 context and 32000 output; the platform runner declares 32768
output. OpenCode's tagged [limit implementation](https://github.com/anomalyco/opencode/blob/53d1eabb61e21162157817bf677da0a4ad3332e3/packages/opencode/src/provider/transform.ts)
explains the second ceiling. Raising limits requires a controlled study of
completion and correctness, with enough time to use the requested budget.

## Research mapped to decisions

Each row states an engineering implication, not a claim that the paper's results
transfer to MiMo or our repositories. Historical scores are not current rankings.

| Primary source and evidence level | Useful finding | Change or experiment for this project |
| --- | --- | --- |
| [SWE-agent, v3](https://arxiv.org/html/2405.15793v3), 2024 interface study | Navigation, editing interfaces and executable feedback affect outcomes | Add a narrow isolated verification tool before publication; measure it against the current file-only worker |
| [Agentless, v2](https://arxiv.org/html/2407.01489v2), 2024 repair pipeline | Localization, repair and validation form a strong simple baseline | Compare a worker plus verifier against the planner/worker/reviewer arrangement under matched conditions |
| [MAST, v3](https://arxiv.org/abs/2503.13657v3), 2025 trace taxonomy | Design, coordination and verification failures require separate diagnosis | Classify failures per stage; do not use one aggregate success count to hide failed reviews or handoffs |
| [CaMeL, v2](https://arxiv.org/html/2503.18813v2), capability and information-flow design | Enforce authority and data flows outside untrusted model text | Keep authorization and publishing in the controller; test actual forbidden effects and disclose policy limits |
| [AgentDojo, v3](https://arxiv.org/abs/2406.13352v3), extensible security environment | Utility and attack success are separate outcomes | Add safe repository/tool-output attacks and measure task completion alongside unauthorized effects |
| [Compact-Bench, v1](https://arxiv.org/html/2610.31710v1), October 2026 preprint | Matched repository-state continuations isolate damage caused by history replacement | Compare full history, compacted history and restored checkpoints from the identical candidate state; report damage and rescue separately |
| [The Compaction Cliff](https://arxiv.org/html/2608.22752v1), 2026 manuscript with CIKM venue reference | Rules and episodic notes need different retention policies | Preserve owner policy and authorization references exactly; verify retained rules rather than trust a summary |
| [A2A failure taxonomy, v2](https://arxiv.org/abs/2610.19342v2), October 2026 preprint | Private peer state and unverified claims complicate coordination | Require explicit ownership and evidence at handoff; our single-owner setting is narrower than the paper's multi-principal setting |
| [SWE-EVO, v6](https://arxiv.org/abs/2512.18470v6), release-level benchmark | Preserving existing behavior across sustained multi-file evolution differs from solving one issue | Include multi-step release and migration tasks with independent regression checks across repositories |
| [METR time horizons, v4](https://arxiv.org/abs/2503.14499v4), task-duration methodology | Human task duration and completion probability are different from time spent generating tokens | Measure meaningful completed work, uncertainty and intervention burden; do not force a task to last 15 minutes |
| [Terminal-Bench 4.0](https://www.tbench.ai/news/terminal-bench-4-0), current benchmark methodology | Resource calibration reduces infrastructure noise; its timeout is eight hours | Calibrate time/CPU/memory headroom and report our constraints before comparing scores across harnesses |

Further angles challenge some tempting shortcuts:

- [TestSuiteBench](https://arxiv.org/html/2610.13065v1), an October 2026
  preprint, evaluates specification-derived test suites against requirement
  mutants. Valid passing tests can still miss intended behavior. Its mutants
  sample requirements and need not match real fault distributions; use mutation
  checks as another signal, not a complete correctness certificate.
- [Rethinking agent-generated tests](https://arxiv.org/html/2602.07900v2)
  finds that changing test-writing volume did not significantly improve issue
  resolution in its controlled prompt interventions. Its light scaffold and
  SWE-bench setting limit transfer. Measure useful fault detection and final
  correctness rather than reward test counts or mandate redundant tests.
- [Test-Time Harness Evolution, v2](https://arxiv.org/html/2607.08124v2),
  revised October 2026, explores evolving executable harnesses from traces
  without final-answer labels. It also identifies unreliable proxy selection:
  an executable candidate can remain semantically wrong. Our experiment should
  propose harness changes through isolated PRs, compare on development tasks,
  then evaluate a sealed holdout before promotion. Authorization, protected
  paths and publication gates stay outside the editable candidate. Automatic
  self-modification is not implemented or justified by this paper alone.
- Anthropic's [long-running harness report](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
  describes incremental work, durable progress artifacts and end-to-end
  verification. Its [managed-agent architecture](https://www.anthropic.com/research/managed-agents)
  separates the event log, harness and replaceable execution environment.
  These are useful engineering precedents for crash recovery, not evidence we
  have invented that architecture. We can study these boundaries without
  purchasing its hosted service.

These sources do not establish that more agents, more tokens or more retained
text always improve quality. Compact-Bench limits its conclusions to its tested
repairs, continuation agent and budgets. The compaction study acknowledges
classification errors and a remaining token-matched behavioral control. CaMeL
acknowledges policy-authoring burden and residual attacks; its reference code is
explicitly a research artifact. Reuse the principles with tests, rather than
advertise their guarantees as ours.

## Open-source choices to revisit

The earlier decision assumed a small VPS, serial one-shot runs and owner-set
labels only. The current host has about 8 GiB RAM, the user wants sustained work,
and trusted autonomous intake has since been authorized. Those changes justify
revisiting components; they do not justify installing every framework.

- [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) (MIT): evaluate
  isolated workspaces and tool feedback as components, with credential isolation
  and dependency review. Hosted paid services are outside scope.
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) (MIT): use as a
  small reference harness with pinned versions and the same task cohort.
- [Agentless](https://github.com/OpenAutoCoder/Agentless) (MIT): reproduce its
  simple pipeline as a control; its older implementation is not a current
  production recommendation.
- [MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code) (MIT): compare a
  provider-native runtime against OpenCode on reasoning transport, tool use and
  recovery before replacing the one-command interface.
- [CaMeL reference](https://github.com/google-research/camel-prompt-injection):
  credit the design; its own README warns against treating it as production-safe.

No new framework or service was installed for this review. No upstream code was
copied into the controller. OpenCode remains credited as the MIT-licensed runtime.
Any later adoption needs pinned source, license notices, dependency checks and
our authorization/publication boundaries preserved.

## First implementation priorities

- Give the worker verifiable feedback before publication. The controller should
  request approved lint/type/test profiles in a disposable environment with no
  model, GitHub or App credentials. Bind results to the exact base and patch;
  an old green result cannot authorize a changed candidate. Environment-variable
  scrubbing alone is not a sandbox against code reading host credential files.
- Treat recovery as a protocol. Bind a checkpoint to task authorization, source,
  candidate, stage, tool policy and runtime version. Fork private state, retain
  the failed attempt, refuse stale or ambiguous state and check again before
  publishing. The [frozen contract](evaluation-2026-10-30/continuation-acceptance.json)
  has 11 cases. Current completed-session transport does not satisfy the
  interrupted-reasoning requirement.
- Separate policy from compactable notes. Reinsert authoritative constraints and
  references through the controller. Record completed work, unresolved questions
  and next actions as data; a model's notes cannot grant privileges or certify tests.
- Add independent correctness evidence. Use baseline regression tests, hidden
  acceptance cases, properties and negative controls alongside native coverage.
  Review test weakening and unrelated edits. For stronger adversarial assurance,
  keep the test driver separate from the process executing candidate code.
- Improve observability without overstating trust. Record effective settings,
  stage outcomes, unknown counters and parent relationships. Existing signatures
  attest ingestion, not independent local execution. Investigate incremental
  verification keyed by content and trust-policy version; a shared D1 schema
  needs inspection and coordination before any changes.

## Experiments and claim threshold

The [frozen experiment plan](evaluation-2026-10-30/experiment-plan.json) separates
output budget from deadline, recovery from compaction, and architecture from
model capability. Keep complete attempt accounting, including timeouts and quota
failures. Codex repairs to MiMo candidates remain separate from autonomous MiMo
results. Operator assistance and synthetic evidence are identified explicitly.

A credible cutting-edge engineering claim needs current methods implemented and
measured on diverse real tasks. A stronger research claim needs a precise gap
relative to prior work, reproducible artifacts, meaningful baselines, ablations,
uncertainty and independent reproduction. Our promising question is whether
verified feedback and recovery can improve completion across repository and
quota boundaries while preserving authorization and correctness. Novelty and
superiority remain hypotheses until those comparisons are done.
