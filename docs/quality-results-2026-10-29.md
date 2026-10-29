# Coding-quality evaluation, 29 October 2026

The native backend produced and repaired an implementation, but the complete
workflow did not finish within its one-hour cycle. The independently checked
revision passes the original six behavior checks, native tests, lint and strict
types. Three additional HTTP metadata checks still fail. This is progress toward
useful coding, not proof of general parity with Codex or Claude Code.
The [manifest](evaluation-2026-10-29/manifest.json) retains record identities,
patch hashes, private-state bindings and the independent check outcomes.

## Controlled task and outcomes

Both shortlink trials used base `d142d64742f0132d5f57471326cc7a6300651721`,
controller `f9f37213fb12e70b19b0d6d4b5ee335b240c89bc` and the unchanged
[six acceptance checks](evaluation-2026-10-29/acceptance.json), originally frozen
before the earlier attempts. Their SHA256 remains
`0f7c911154dd3fd8a9afa8445c678ceab429118a41e82d91aa85b973e85ac669`.
The baseline still fails five and passes one. No evaluator repaired application
code, reset provider/repair history or merged an application PR.

| Attempt | Observed result |
| --- | --- |
| Existing autonomous [ello39](https://github.com/Abdel-Abdelaal/ello/issues/39), run31 | Legacy MiMo planner exhausted its easy turn allowance after 422.204 seconds. No implementation. Its criteria were not frozen before it started; this is operational evidence, not a held-out Android benchmark. |
| [shortlink62](https://github.com/Abdel-Abdelaal/shortlink/issues/62), run32 | Legacy MiMo/Claude Code planner timed out after 1,200.158 seconds. No implementation, candidate checks or PR. |
| [shortlink63](https://github.com/Abdel-Abdelaal/shortlink/issues/63), run33 | Native MiMo/OpenCode planned, implemented, passed the diff gate, rejected its initial code and repaired the redirect logic. The cycle expired during the final review. No completed final native verdict or application PR. |

Completed native phases reported 971.693 seconds planning, 692.846 implementing,
1,067.105 reviewing and 599.118 repairing. The controller reused the completed
plan when the same MiMo model's difficulty increased. The maximum one-hour cycle
left insufficient room for the permitted second review. The interrupted result
remains a failure; future local cycles now allow up to two hours within the
existing configuration ceiling. Per-call deadlines and review/repair counts are
unchanged. No new automatic retry or paid capacity was enabled.

## Independent code checks

The evaluator applied each controller-gated patch unchanged to an isolated copy
of the fixed base. Patch hashes identify these pre-publication snapshots; they
are not published PR commits. The agent/reviewer workspaces were untouched.

| Check | Initial snapshot | Repaired snapshot |
| --- | --- | --- |
| Original frozen behavior checks | 6 passed | 6 passed |
| Native tests | 65 tests and 5 subtests passed | 65 tests and 5 subtests passed |
| Branch-aware coverage | 100% | 100% |
| Ruff lint, formatting, strict Pyright | Passed | Passed |
| Additional protocol checks | 5 failed | 2 passed, 3 failed |
| GitHub candidate CI | Not run: no published PR | Not run: no published PR |

The [supplemental checks](evaluation-2026-10-29/protocol-posthoc.json) were written
after inspecting the initial candidate. They are explicitly separate from the
original pre-task frozen six and are not a blinded benchmark.

The native reviewer independently identified that HEAD and GET used different
redirect construction. Its repair introduced one shared `redirect_response`
helper, fixing both independently reproduced cases using accepted pipe/caret
URLs. The reviewer's example using an already escaped `%20` URL did not itself
reproduce the difference; the underlying finding was confirmed with other inputs.
The reviewer received none of the evaluator's findings before its decision.

The remaining problem is `Content-Length: 0` on health and error HEAD responses
whose corresponding GET representations are nonempty. The health GET body is
15 bytes. [RFC 9110 section 8.6](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.6)
allows omitting that header on HEAD; if supplied, it must describe the
corresponding GET representation. The repaired snapshot remains unsuitable for
merge despite passing its existing tests. Expiry validation was appropriately
shared in the domain service; HTTP code did not duplicate that policy.

## What telemetry revealed

Native raw state survived every phase, including the interrupted final review.
The CLI/MCP inspector originally searched only agent-clone profiles, missing the
reviewers' publication-clone profiles. This change includes all review attempts
without exposing prompts, reasoning, paths or tool inputs/outputs. Its regression
test reproduced the omission before the fix and checks neighbouring-run exclusion
and confidentiality. Full controller verification passes 342 tests with 99.66%
local coverage, strict types, lint, dependency and architecture checks.

Native traces also show unsuccessful searches for dependency sources outside the
workspace. Access remained blocked; this is a possible reference-context gap,
not proof of the cause of any timeout. The model was not given arbitrary shell
or host-file access. Application checks above were executed by the independent
evaluator, not inferred from model claims.

The evaluator's separate rejection record binds the accepted fixtures, both
patches, native prompts and retained logical native-state hashes. It has its own
Codex-desktop harness identity; it is not a Codex CLI dispatch, a resumed native
review or an alteration of the failed run. Only hashes are exported for the
retained private payloads. Signatures attest hosted ingestion of controller
reports, not independent proof of local execution. Ordinary native conversations
are still not automatically exported to the public dashboard.

## Reasoning configuration and compatibility

The initial legacy reference recorded the requested native reasoning setting.
Source inspection clarified that it does not control the Claude Code gateway.
The gateway's zero thinking variable can omit the request parameter; Xiaomi's
[Anthropic API reference](https://mimo.mi.com/docs/en-US/api/chat/anthropic-api)
documents server thinking as enabled by default. Effective server-side legacy
reasoning is therefore not inferred from that variable.

An unshipped settings-only proposal passed unit checks but the actual installed
Claude Code 2.1.296 sent `thinking: adaptive` to a local synthetic endpoint.
MiMo's published request schema lists `enabled` and `disabled`. This request
format difference was not deployed, and no real-provider acceptance or rejection
is claimed. A separate correct two-turn SSE fixture confirmed that both tested
client configurations preserved returned synthetic reasoning and file-tool results.
See the [request summaries](evaluation-2026-10-29/claude-wire.json) and
[conversation summaries](evaluation-2026-10-29/claude-conversation-wire.json).
These use dummy credentials and no live model calls. The native backend uses
its already verified explicit OpenAI-compatible reasoning configuration.

OpenCode remains the installed chat interface. The native coding backend is an
explicit local pilot; the source default remains unchanged. Xiaomi's MIT-licensed
[MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code) was researched as another
possible comparison, but was not installed or incorporated in this trial.
