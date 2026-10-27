# Native harness evaluation, 26–27 October 2026

The new native application test did not produce an implementation or PR. It
cannot establish application quality comparable to Codex or Claude Code. Longer
reasoning was allowed; token counts and elapsed time are not quality scores.

## Fixed application task

Add HEAD health/link previews without counting hits. Preserve GET counting,
expired-link behavior, metadata access and domain boundaries; reuse the existing
expiry policy. The [six acceptance checks](evaluation-native-2026-10-27/acceptance.json)
were frozen before either attempt. Original baseline
`d142d64742f0132d5f57471326cc7a6300651721` failed five and passed one.
The [manifest](evaluation-native-2026-10-27/manifest.json) retains revisions,
configuration, frozen hashes and outcomes. No candidate code was manually edited.

| Attempt | Observed result | Interpretation |
| --- | --- | --- |
| [shortlink58](https://github.com/Abdel-Abdelaal/shortlink/issues/58), run27 | Legacy planner capped at768.193 seconds, restarted without its prior conversation, then operator-cancelled at1128.062 seconds. No implementation/PR. | Failed evaluation; history retained. Restarting a discarded conversation was a harness defect, not evidence that reasoning itself was bad. |
| [shortlink60](https://github.com/Abdel-Abdelaal/shortlink/issues/60), run28 | Actual native front door queued and started the task. MiMo OpenCode reasoning enabled; planner recorded timeout124 before implementation. No implementation/PR. | Candidate acceptance and CI were not run because no candidate existed. Do not count baseline failures as candidate failures or declare the run successful. |

Run28 used controller `58c6a2cdad936340a3faf2fed61509c43adc0598`, with a one-hour
cycle allowance and the existing20-minute per-call budget. Reported planner wall
duration was33,655,801ms, exceeding both configured budgets. Host suspension or
clock behavior may affect native waits; the exact cause has not been established.
There is no evidence of nine hours of productive model work.

Completed native steps reported33,601 input tokens,789 output tokens and36,089
reasoning tokens. Interrupted requests may have additional unreported usage;
these counters are not a complete billing total. During the run, native metadata
showed20 file/search tool calls, with at least one read error, and an eleventh
step awaiting a final response. The application clone had no edits when inspected.

## Defects identified and corrections

PR35 stopped read-only cap restarts, retained repair allowances, preserved selected
harness settings and made reasoning explicit. Reasoning remains enabled by default.
The pinned OpenCode adapter now passes reasoning state between tool calls;
an actual executable/synthetic-endpoint test confirmed the round trip without
live inference. That is transport evidence, not application quality evidence.

Run28 exposed two further defects: native diagnostic state was temporary, and
long blocking waits did not promptly honor the recorded wall-clock deadline.
The follow-up keeps private native state durably from call start and rechecks UTC
deadlines every five awake seconds. Its read-only `inspect_run` tool combines
phase/usage metrics with native tool counters, excluding raw confidential payloads.
See [the run evidence guide](native-run-evidence.md) for privacy and resume limits.
The already-deleted run28 native conversation cannot be reconstructed.

A later autonomous task, [.github31](https://github.com/Abdel-Abdelaal/.github/issues/31),
run29, completed native planning and implementation but attempted12 protected
workflow/template edits. The controller correctly refused the diff before push;
no PR was created. The task offered a documentation alternative, yet the planner
and worker chose forbidden workflow changes. This is a quality failure caught by
a working gate. Follow-up prompts now derive the write restrictions directly
from the gate's canonical pattern list for planning, implementation and review,
including nested template copies. The gate itself is unchanged. Prompt changes
are not proof that future models will obey; the diff gate remains mandatory.

## Existing quality evidence remains mixed

The [earlier independent evaluation](quality-results-2026-10-26.md) recorded a
refactor passing four behavior/reuse checks, native tests, required CI and100%
coverage. Another bug fix passed four behavior checks but failed required types;
the model review incorrectly approved it. A request to weaken checks/disclose
credentials was correctly refused. Those are specific results, not proof of
universal code quality or parity with stronger coding agents.

Further live attempts should preserve diagnostic state and use the same fixed
acceptance checks. Compare code correctness, reuse, regressions, review accuracy,
required CI and known usage before selecting a workflow or reasoning mode. No
counter resets, hidden retries, manual candidate repair or paid capacity are part
of this evaluation. Diagnostic tests and transport smokes do not replace it.
