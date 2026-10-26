# Attempt telemetry

agent-dispatcher writes one JSON record per attempt to
`runs/telemetry/attempts-YYYY-MM.jsonl` on the controller host. The file is local,
append-only and gitignored. The dispatcher never signs, redacts or publishes it. The
platform's AI telemetry tooling (platform-workflows `tools/ai_*`, owned by Codex)
ingests, redacts, signs and publishes it. Model processes never get the GitHub token or
any signing material.

Schema: `agent-dispatcher.attempt/v1`.

## Stages

| Stage | Who | Outcomes |
| --- | --- | --- |
| `route` | controller, before the run: one record per provider the team left out | `skipped` (with `reason` and `resets_at`) |
| `triage` | MiMo or NVIDIA chat API | `proceed`, `declined`, `unavailable`, `skipped` |
| `plan` | thinker, read-only | `ok`, `refused`, `failed`, `exhausted` |
| `implement` | worker | `ok`, `failed`, `exhausted` |
| `gate` | controller | `passed`, `refused` |
| `review` | the other thinker, read-only | `approved`, `rejected`, `invalid_json`, `failed`, `exhausted` |
| `repair` | worker, on the review's findings | as `implement` |
| `ci_repair` | worker, on a failing CI log | as `implement` |
| `ci_review` | thinker, on the CI repair | as `review` |
| `publish` | controller | `ok`, with `pr_url` |

A run that stops early has no records after the stop. The issue's `agent:*` label and
comment show why.

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `schema`, `record_id`, `recorded_at` | string | schema name, a UUID, and when the record was written (UTC) |
| `run_id`, `attempt` | integer | the run in the queue, and the record's order in it (route records use 0) |
| `repo`, `issue`, `branch`, `pr_url` | string, integer | the joins to GitHub; `branch` and `pr_url` once known |
| `stage`, `provider`, `harness` | string | see above; harness is `claude-code`, `codex-cli`, `opencode` or `chat-api` |
| `model`, `effort`, `level`, `read_only` | string, boolean | the model and effort actually requested, and the task's difficulty level at the time |
| `started_at`, `finished_at`, `duration_ms` | string, integer | timing of a model call |
| `prompt_sha256`, `prompt_chars` | string, integer | the prompt's hash and size; the prompt itself is never stored |
| `context_sha256` | object | hashes of the inputs: `issue`, `plan`, `diff` or `ci_log` |
| `outcome`, `exit_code`, `exhausted`, `resets_at` | string, integer, boolean | how the attempt ended |
| `usage` | object or null | tokens and cost exactly as the CLI reported them; null when the harness reports none |
| `available_usage` | null | always null until a provider offers a headless reading; unknown stays unknown |
| `output_sha256`, `output_chars`, `output_excerpt` | string, integer | the model's final text by hash and size, plus at most 2,000 characters |
| `gate_refusals`, `files`, `lines` | list, integer | gate records: each refusal, the changed paths (at most 20), and changed lines |
| `reason` | string | route records: `parked`, `unused` or `not configured` |

## What is never recorded

- Prompts.
- Issue bodies, plans, diffs or CI logs in full. They appear only as hashes.
- Environment variables, tokens, keys or login files.

Excerpts are model output and therefore untrusted. The ingestion side must redact them
before anything is published.
