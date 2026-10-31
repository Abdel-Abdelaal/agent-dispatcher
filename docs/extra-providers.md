# Extra providers

Any model behind an OpenAI-compatible API can join the team through one setting,
with no adapter code. Each entry runs through the same harness MiMo uses: the
pinned [OpenCode](https://opencode.ai) CLI with its open-source
[`@ai-sdk/openai-compatible`](https://ai-sdk.dev/providers/openai-compatible-providers)
adapter. It gets the same locked-down config (no shell, no web, no MCP, no
`.env` reads), and the same gates, review, diff caps and draft-only publishing.

## Add one

Set `DISPATCHER_EXTRA_PROVIDERS` in `.env` to a JSON list:

```dotenv
DISPATCHER_EXTRA_PROVIDERS=[{"name":"openrouter","base_url":"https://openrouter.ai/api/v1","model":"qwen/qwen3-coder","api_key":"sk-or-...","context":262144,"output":32000}]
```

| Field | Meaning |
| --- | --- |
| `name` | Lower-case provider name used in labels, telemetry and `DISPATCHER_DISABLED_PROVIDERS`. It can't reuse `claude`, `codex`, `mimo` or `nvidia`. |
| `base_url` | The API root that serves `/chat/completions`. Must be `https`, or `http` to `localhost`, `127.0.0.1` or `::1`. |
| `model` | The model ID exactly as the endpoint names it. |
| `api_key` | Sent only in the OpenCode process environment, never on the command line or in config, and scrubbed from telemetry. A local server ignores it, so any value works there. |
| `role` | `worker` (default) implements. `thinker` can also plan and review. |
| `context`, `output` | The model's real token limits. OpenCode compacts the conversation against them. Defaults are 128000 and 16000. |

Unknown fields are refused, so a typo can't silently do nothing.

## Where it sits in the team

Extras come after the built-in providers of their role. Thinkers are `claude`,
`codex`, then extra thinkers. Workers are `mimo`, `nvidia`, then extra workers.
An extra steps in when the providers ahead of it are parked or disabled.

To run on one extra provider alone, disable the others and allow worker
thinking. The extra then plans, implements and reviews in separate calls,
the same way MiMo-only mode works:

```dotenv
DISPATCHER_DISABLED_PROVIDERS=claude,codex,mimo,nvidia
DISPATCHER_ALLOW_WORKER_THINKING=true
```

A provider that answers with a rate-limit or quota error (for example HTTP 429)
is parked until it resets, like any other.

## Examples

These are the documented OpenAI-compatible endpoints. Check the provider's own
page for current model IDs, limits and free-tier terms before using one.

| Service | `base_url` |
| --- | --- |
| [OpenRouter](https://openrouter.ai/docs/quickstart) | `https://openrouter.ai/api/v1` |
| [Groq](https://console.groq.com/docs/openai) | `https://api.groq.com/openai/v1` |
| [DeepSeek](https://api-docs.deepseek.com/) | `https://api.deepseek.com/v1` |
| [Mistral](https://docs.mistral.ai/api/) | `https://api.mistral.ai/v1` |
| [Google Gemini](https://ai.google.dev/gemini-api/docs/openai) | `https://generativelanguage.googleapis.com/v1beta/openai` |
| [Ollama](https://github.com/ollama/ollama/blob/main/docs/openai.md), local | `http://localhost:11434/v1` |
| [LM Studio](https://lmstudio.ai/docs/app/api/endpoints/openai), local | `http://localhost:1234/v1` |
| [vLLM](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html), self-hosted | your server's `/v1` |

For a provider without an OpenAI-compatible API, run a
[LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy). It translates 100+
providers to one OpenAI-compatible endpoint. Point an entry at the proxy
instead of adding code here.

## Limits

- Coding quality varies a lot between models. A weak model can produce plans or
  diffs that the gate or review rejects, and that run is recorded as failed. The
  gates protect the repository; they don't make a model capable.
- Usage shares aren't read from these endpoints. As with MiMo, a provider stays
  open until it reports exhaustion.
- Triage still uses MiMo, else NVIDIA. With neither, triage is skipped and the
  planner rates the task.
- Only the OpenAI-compatible adapter is used. OpenCode can install other AI SDK
  packages at runtime, but this controller doesn't let a config entry download
  code.
