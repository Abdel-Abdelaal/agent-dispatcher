# Native call bindings

Controller-run MiMo/OpenCode calls retain a private `metadata.json` outside the
agent clone before starting the native process. Its `binding` identifies the run
and stage, a hash of the raw task identity/title/body, the base from trusted Git
metadata, the starting candidate patch hash and the full diff policy hash.
`config.sha256` binds the native permissions, model, reasoning and step settings.
The existing prompt hash binds the exact call instructions and attached context.
Raw task text and credentials are excluded from these identity fields.

The base and patch use the controller's external Git metadata. An agent changing
its own HEAD, index or Git configuration cannot substitute a different base.
Every call, including an escalation, receives a fresh binding; the shared provider
adapter is not mutated. Direct adapter calls without controller context explicitly
retain `binding: null`. Existing diagnostic profiles remain unbound.

These are **starting-state receipts, not resumable checkpoints**. No continuation
command, automatic restart or publication authorization is enabled by them.
Safe continuation still requires a final candidate receipt, native session and
version validation, path and policy checks, fresh task authorization, an owned
cycle lease and preservation of all failure, usage and quota history. A receipt
does not authenticate its own contents or prove that an agent completed a task.
Useful interrupted-response continuation also requires a real-provider experiment
against the frozen acceptance contract in `evaluation-2026-10-30/`.

Regression tests use real local Git and fake native processes; they make no model
calls or GitHub writes. They test provenance boundaries, not model quality.
