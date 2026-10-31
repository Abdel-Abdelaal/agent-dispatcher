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
| `schema`, `schema_version`, `record_id`, `recorded_at`, `controller_revision` | string, integer | schema name and version, a UUID, the write time (UTC), and the controller's commit |
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
| `output_sha256`, `output_chars` | string, integer | the full output's hash and size, always kept |
| `output_excerpt`, `excerpt_status`, `excerpt_truncated` | string or null, string, boolean | at most 2,000 scanned characters, or `null` with the reason |
| `gate_refusals`, `files`, `lines` | list, integer | gate records: each refusal, the changed paths (at most 20), and changed lines |
| `reason` | string | route records: `parked`, `unused` or `not configured` |

## Privacy before retention

Nothing reaches the file before these rules apply:

1. **Credentials are scrubbed.** Every credential the controller holds (GitHub token,
    Claude token, MiMo and NVIDIA keys), plus common token shapes, is replaced with
    `[REDACTED]` in every string field.
2. **Excerpts need a scanner.** An output excerpt is kept only after the trusted
    scanner at `DISPATCHER_OUTPUT_SCANNER` has passed it. That's an absolute path to
    a CLI that reads text on stdin, prints the scanned text and exits 0. A relative
    path is refused at startup instead of quietly turning the scanner off.
3. **Hash and length always stay.** Without a scanner, or if it fails, the excerpt
    is `null` and `excerpt_status` says why.
4. **Private permissions.** The spool directory is created `700` and the file `600`.
    On Windows that only sets read-only; the file inherits the user profile's ACLs.

| `excerpt_status` | Meaning |
| --- | --- |
| `clean` | scanned, nothing changed |
| `redacted` | scanned, and something was masked |
| `omitted_no_scanner` | no scanner configured, so only the hash and length are kept |
| `omitted_scanner_failed` | the scanner failed, so only the hash and length are kept |
| `empty` | the model returned no text |

Every record also has `schema_version` (1) and `controller_revision` (the controller
checkout's git commit, or `unknown`). Output records have `excerpt_truncated`.

## What is never recorded

- Prompts.
- Issue bodies, plans, diffs or CI logs in full. They appear only as hashes.
- Environment variables, tokens, keys or login files.

Excerpts are model output and therefore untrusted. The ingestion side must redact them
before anything is published.
