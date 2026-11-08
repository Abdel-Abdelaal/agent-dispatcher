# Harness research update — 8 November 2026

This follows the [6 November update](harness-research-2026-11-06.md). Abdel asked
whether we should take the best parts of other open-source agent projects
(OpenHands, Goose, OpenCode, SWE-agent, ProPR, LangGraph, Letta, elizaOS) instead
of building them ourselves. The [build-vs-adopt decision](decisions/0001-build-vs-adopt.md)
answered that for most of them on 25 October; this note checks it again, adds
the projects it didn't cover, and says what we take.

The short answer: we already run on the part that matters most (OpenCode), and
we copied the ideas that moved results (executable feedback, reproduce-first
tests). Merging whole frameworks would add code we must keep safe without
raising the score. What's left worth taking is a few specific features, below.

## Project by project

Licences and activity were read from GitHub on 2026-11-08.

| Project | Licence | What it offers | Where we stand | Verdict |
| --- | --- | --- | --- | --- |
| [OpenCode](https://github.com/anomalyco/opencode) | MIT | coding-agent runtime with file tools, any OpenAI-compatible model | our worker, reviewer and planner run on it (pinned 1.18.35) | adopted |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent), [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | MIT | agent-computer interface, executable feedback | the sandbox `run` tool and the reproduce-first instruction come from this line of work | ideas adopted; no code needed |
| [Agentless](https://github.com/OpenAutoCoder/Agentless) | MIT | localise, repair, validate; sample several patches and pick one with tests | untaken: patch sampling and selection | see "Best of N" |
| [OpenHands](https://github.com/OpenHands/software-agent-sdk) | MIT | agent SDK, isolated workspaces, context condensing | we have our own Docker sandbox; rejected as a whole on 25 October | keep rejected; condensing noted below |
| [Goose](https://github.com/aaif-goose/goose) | Apache-2.0 | general agent runtime with MCP extensions | overlaps OpenCode; nothing it adds would raise coding results | keep rejected |
| [ProPR](https://github.com/integry/propr) | Apache-2.0 | label an issue, get a PR from an agent in its own container; `/review` and `/fix` comments on the PR push follow-up commits | same shape as this controller, in TypeScript; small project (about 30 stars) | take the PR-comment follow-up idea, not the code |
| [LangGraph](https://github.com/langchain-ai/langgraph) | MIT | durable, resumable agent graphs | our pipeline is linear, with SQLite leases and checkpoints; a graph engine adds no result | reject |
| [Letta](https://github.com/letta-ai/letta) | Apache-2.0 | long-lived agent memory | each run is one task; per-repo memory isn't what loses tasks today | reject for now |
| [elizaOS](https://github.com/elizaOS/eliza) | MIT | social and character agents with plugins | outside a coding controller's scope | reject |

## What the newer research says

The biggest documented gain for a fixed model is still sampling several
candidate patches and picking one. [SWE-Replay](https://arxiv.org/pdf/2601.22129)
samples ten patches per issue on mini-SWE-agent and picks with an
Agentless-style step. [SWE-HERO](https://arxiv.org/pdf/2604.01496) samples 32
and reports that the gap between the best candidate and the one its verifier
picks grows with the count: choosing is the weak point, not generating.
Analyses of 2026 leaderboards ([one example](https://particula.tech/blog/agent-scaffolding-beats-model-upgrades-swe-bench))
argue that scaffolding moves scores more than model upgrades. Those are blog
figures, self-reported and not checked by us.

## What we take, in order

1. **Keep what a failed call said** (#95). Bench 7 lost a correct fix to two
   review calls that failed with no reason, and nothing recorded why. Diagnosis
   comes before any fix.
2. **Best of N on hard tasks, picked by our own checks.** The one lever the
   research agrees on. A second worker attempt from the same plan would run only
   at the hard level, and the controller would pick between candidates with the
   sandbox checks and the task's reproduction test before the reviewer sees one.
   It roughly doubles tokens on hard tasks (the most expensive tasks in the
   current run used 300k to 510k each), so it needs Abdel's go-ahead before
   it's built.
3. **Follow-up commits from PR comments (from ProPR).** Today an agent's draft PR
   waits for Abdel, and a review comment does nothing. An owner comment such as
   `/fix <what>` on an agent PR would start a repair run on that branch under the
   same gates. This changes the flow, not the benchmark score.
4. **Context condensing (from OpenHands), measured first.** Calls that hit their
   turn cap are continued at a higher level today. If traces show continued calls
   failing because they lost context, condensing is the next thing to try.

Nothing was installed and no upstream code was copied for this review.
