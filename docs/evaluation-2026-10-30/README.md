# Evidence interpretation and verification

These records are local synthetic protocol observations, not live inference or
an independently reproduced benchmark. Original summaries remain unchanged.
The following map distinguishes the procedures; the original capture timestamps
were not recorded and are not reconstructed from file modification times.

| Record | Experiment identity and procedure |
| --- | --- |
| conversation-fork.json | conversation_fork_only: complete a synthetic text response, then fork and inspect history and original-source hashes |
| completed-reasoning.json | fork_with_completed_reasoning: complete separate synthetic role/reasoning chunks, then fork and inspect retained and replayed reasoning |
| interrupted-reasoning.json | interrupted_reasoning_controlled: interrupt the same chunk protocol before completion, then fork and inspect marker retention |
| interrupted-reasoning-shapes.json | interrupted_reasoning_value_shape_recheck: new independent repetition recording capture time and next-request reasoning field types and lengths |
| fixture-assumption-error.json | first_conversation_fork_assertion_error: initial conversation-fork probe assumed parent_id had to name the source; the corrected conversation_fork_only probe checks actual preserved content instead |
| output-budget-wire.json | output_budget_declaration_matrix: inspect outgoing max_tokens under three declared-model/global-limit combinations |

In the original interrupted summary, the replay boolean tests whether the exact
synthetic marker occurs anywhere in the next messages. Field names alone do not
establish whether their values are empty. The separate value-shape repetition
records the assistant's reasoning_content as a string of length zero, even
though its key is present. Its roles and keys were independently observed in
that new capture, not copied from the original result. This supplies direct shape evidence for the repeated
observation; it does not retroactively capture the original request values.

The output matrix label `current` means the platform runner's 32768 declaration,
not the native helper's 32000 declaration. No captured case directly measures
the helper's 32000 declaration. The report's native setting comes from source
inspection; the matrix measures the platform declaration and override cases.

The original phrase "stale-candidate guards" means proposed controller gates;
none of these transport probes establishes those gates or production recovery.

## Hash inputs

SHA-256 covers the UTF-8 file content published by Git with LF line endings,
without a Git blob header. For example, pipe the bytes returned by
`git show HEAD:docs/evaluation-2026-10-30/conversation-fork.json` into SHA-256.
It is not Git's object identifier. The manifest also binds the frozen experiment
plan and continuation acceptance contract. Run
`python -m pytest tests/test_research_evidence.py` to verify all listed files.
The test also rejects JSON records omitted from the manifest and manifest
entries without a corresponding file. It normalizes checkout CRLF line endings
to the published LF representation. The co-committed manifest detects accidental
drift; it is not an authenticated statement against someone changing both a
record and its hash. Independent source binding is supplied separately through
the signed telemetry pipeline, whose signatures attest ingestion.
Original local result/script hashes are separately recorded in telemetry.
