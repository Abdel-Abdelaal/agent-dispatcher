# Harness research update — 6 November 2026

This follows the [30 October review](harness-research-2026-10-30.md). It records
what the benchmark now shows, what recent work on coding-agent harnesses
suggests, and which of those ideas we take, defer or reject, with the reason.

## Where the benchmark stands

All runs use MiMo v2.6 Flash for every role, with the sandbox, on the
[benchmark](benchmark.md) tasks.

| Run | Controller | Resolved | Tokens |
| --- | --- | --- | --- |
| Bench 2 | with #81 | 9 of 13 | 1.65M |
| Bench 3 | `935c362` (#82) | 11 of 13 | 1.65M |
| Bench 4 | `60dc897` (#83 to #86) | 12 of 13 | 1.66M |

Bench 3's `sh-head-metadata` failure (a reply cut at the output limit with no
edits) passed in bench 4 after #86. `pw-scan-context-secrets` failed in both
runs the same way: the worker JSON-escaped the repository context before
scanning it, which breaks the scanner rule the fix relies on. We read that as a
model judgement error, not a harness fault, and the task stays as written.

With 13 tasks one result moves the score by 8 points, so #89 grew the set to 25.
Bench 5 measures `38b4d62` (#87 and #88) on all 25.

Where the time goes: in bench 4's three slowest tasks (about 45 minutes and
250k tokens each), model reasoning took nearly all the time. On
`sh-head-metadata` the planner spent 712 seconds and the first implement call
976 seconds and 35k reasoning tokens before each reached its turn cap.
Exploration turns were not the bottleneck.

## What recent work suggests

- A [source-code study of eleven harnesses](https://arxiv.org/html/2609.00006)
  (Claude Code, Codex CLI, Gemini CLI, OpenHands, Aider, Mini-SWE-Agent,
  OpenCode and others) finds that loop sophistication does not predict benchmark
  results. In its ablation, gains came from tools, middleware and memory, not the
  system prompt alone. None of the systems retrieves code with vector embeddings.
- [SWE-Serve](https://arxiv.org/pdf/2609.26777) reports that the bash-only
  mini-SWE-agent matched or beat the vendors' own harnesses with the same
  models, and uses it as its leaderboard harness.
- A [taxonomy of operational safety failures](https://arxiv.org/html/2605.30777v3)
  in agentic coding tools, built from 547 confirmed failures, is a checklist for
  our gates rather than a coding-quality method.

We read these as a direction, not a result that transfers to MiMo Flash: prefer
better feedback and tools over more prompt text, and measure every change on the
benchmark.

## Decisions

| Technique | Status | Reason |
| --- | --- | --- |
| Reproduce the bug with a test first | Have (#88) | Standard in strong harnesses; measured in bench 5 |
| Verify before finishing | Have | The controller runs the repository's checks after every edit pass and asks for a repair |
| Treat a reply cut at the output limit as unfinished | Have (#86) | Lost bench 3's `sh-head-metadata`; passed in bench 4 |
| Turn a capped planner's notes into a plan | Have (#87) | Planner hit its step cap in 5 of 13 bench 4 tasks |
| Lint and type diagnostics in every edit result (OpenCode LSP, Aider lint-on-edit) | Rejected for now | Language servers would run on the controller host, outside the sandbox, and OpenCode downloads them itself. The worker can run ruff and pyright through the sandbox tool already |
| Repository symbol map in the prompt | Deferred | Only worth it if bench 5 shows turns spent finding code; bench 4 does not |
| Best-of-N patches with a selector | Deferred | Costs N times the tokens on a paid plan; needs the owner's decision on spend |
| Stop repeated identical tool calls | Check | Our config denies every permission it doesn't allow, which should cover OpenCode's repeated-call prompt; not yet tested |

## Maintained sources of coding rules

These are kept up to date by their projects, so we point at them rather than
copy them:

- [Ruff's rules](https://docs.astral.sh/ruff/rules/), which bundle flake8-bugbear,
  pylint and others. Every fleet repository already runs its own Ruff
  configuration in the sandbox checks.
- The [Semgrep rule registry](https://semgrep.dev/docs/running-rules), maintained
  by Semgrep and contributors under the Semgrep Rules License. A candidate for an
  extra sandbox check, pinned by commit; it would add a dependency and catches
  security patterns more than behaviour bugs.
- The [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) and the
  yearly [CWE Top 25](https://cwe.mitre.org/top25/), as references for the
  reviewer's security questions.

## Next

Read bench 5's failures one by one and fix what the harness got wrong, as for
bench 3 and 4. Raise the turn caps or add a repository map only if the traces
show the need.
