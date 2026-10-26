# Controller cycle deadlines

The trusted host now runs `agent-dispatcher supervise`, which starts its own
`once` child. `DISPATCHER_CYCLE_TIMEOUT_SECONDS` defaults to1800 (30 minutes,
configurable60–7200). It bounds the controller child across planning, coding,
review, publication and repairs. Native chat sessions are separate.

The supervisor owns the process it creates and reuses the existing process-tree
cleanup on timeout. It never finds and terminates arbitrary existing PIDs. A
random lease connects that child to its run. SQLite retains its PID, creation
time and absolute deadline across restarts. Only exact-lease unfinished runs can
be completed by cleanup. Existing history, repair allowances and parked-provider
state are preserved; no quota or retry allowance is reset.
CI repair allowance is reserved before model work and finalized afterward, so a
terminated controller cannot silently regain a repair attempt.

An expired lease can be recovered when the exact child is confirmed dead. PID
reuse is checked using process creation time. A live or inaccessible process
keeps the lock: uncertain ownership is not authority to kill another process or
start duplicate work. Killing the supervisor alone while its child stays alive
therefore requires operator investigation. Process identity metadata supports
safe recovery; it is not resumable worker conversation state.

Read-only planning and review stop when their turn cap is exhausted. Restarting
those conversations throws away their reading and repeats work. Coding workers
can still escalate within their existing limits while retaining file changes.
No deadline failure automatically requeues a task. The owner must deliberately
authorize a retry after inspecting the failure.

The MiMo OpenCode worker now explicitly disables thinking and auxiliary providers,
matching the native UI's verified request format. Its previous exploratory
measurement remains failed. A separate bounded file-tool smoke completed correctly
in14.922 seconds with zero reported reasoning tokens; this verifies the adapter,
not general application quality. The HEAD application task is evaluated separately.

Valid Conventional task titles are preserved instead of receiving a second
`chore:` prefix. Bug/enhancement/documentation labels provide a fallback type.
Raw model plans and review prose are no longer copied into PR descriptions;
the description links the task and includes a plan hash. Existing scanned
telemetry remains the output evidence. Credential-like diffs are refused before
push, and held secrets are redacted from failure reasons before issue comments.

After termination, local run state and hash-based telemetry are retained. GitHub
label/comment cleanup is bounded and best effort, so a network failure can leave
the remote label stale; local history remains authoritative about the timeout.
Any existing draft PR must still be checked independently. Cleanup does not merge
or close it. The controller budget excludes host audit intake, export and final
notification work. It does not guarantee code quality or completion by deadline.

The canonical [host script](../scripts/host-cycle.ps1) retains trusted audit intake
and telemetry export. The laptop's local launcher calls that protected-main script
with its existing trusted scanner path. Never put a key in either script.

## Open-source dependency

[psutil](https://github.com/giampaolo/psutil), pinned7.2.2, supplies cross-platform
process identity checks. It is BSD-3-Clause licensed; its distribution retains
the upstream license. Its release predates the repository's14-day cooldown.
Typing uses the matching typeshed stubs. No upstream implementation was copied.
OpenCode remains the existing pinned native interface; neither service purchases
nor paid capacity are enabled by this change.
