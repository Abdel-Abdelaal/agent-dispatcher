# Review a native worker run

Inspection includes profiles from planning/coding clones and each review's clean
publication clone. Earlier inspection matched only agent-clone profiles, hiding
retained reviewer activity. The counters still exclude raw prompts, reasoning,
file paths and tool payloads.

In `mimo`, ask: “Inspect run28 for shortlink, explain what failed, then check
any resulting PR and its latest CI.” The native `trifecta_inspect_run` tool
checks that the repository is owned and public before reading the corresponding
local run. A run from another repository is refused.

The report shows controller phase outcomes, recorded durations and known token
usage. New OpenCode runs also provide retained native step counts and tool
completion/error counts. It excludes raw prompts, reasoning, file paths and
tool inputs/outputs. It does not independently prove that code is correct.
Use `inspect_pr`, actual diffs, independent regression checks and latest-head
CI to assess implementation quality. Same-model review remains less independent
than review by another provider or independent tests.

From a local terminal, the same read-only report is available with:

```powershell
agent-dispatcher evidence 28 --repo Abdel-Abdelaal/shortlink
```

## Private evidence and interruptions

The configured MiMo OpenCode adapter now keeps its prompt, native data directory
and returned raw events under `runs/checkpoints/run-N-agent-<unique>/`.
The directory is separate from the model's editable clone. Configuration,
bootstrap cache and temporary directories remain ephemeral. Provider keys are
supplied through environment variables, not written into the metadata.

Native state is created durably before inference starts, so killing a controller
does not rely on its exception handler running to preserve the native database.
An interrupted call may have partial state and no final receipt. A missing or
unreadable database is reported as unavailable, never inferred as success.
Older runs whose temporary state was already deleted cannot be reconstructed.

These are local confidential payloads. They inherit local filesystem access,
are not encrypted by this feature, and are not uploaded by the public telemetry
sender. The Git-ignored `runs/` directory must stay out of commits and public
artifacts. No automatic deletion or cloud storage is enabled.

Retention provides diagnostic context, not validated automatic conversation
resume. Do not promote or requeue a failed task just to hide its history.
Inspect existing files and any draft PR before a deliberate continuation.

Subprocess waits now recheck absolute UTC deadlines at most every five awake
seconds. This handles a clock jump or host suspension without waiting an entire
remaining native wait interval after wake. A sleeping laptop cannot execute
checks; notification still depends on the host and network becoming available.
Only an owned child can be terminated; these changes do not attach to old PIDs.

The harness keeps reasoning enabled. Long reasoning is neither a correctness
failure nor proof of progress. Evaluate the retained events, actual code,
regressions, reuse and CI together before changing the model or reasoning mode.
