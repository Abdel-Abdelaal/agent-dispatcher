"""Provider adapters: Claude Code, Codex and OpenCode, plus OpenAI-compatible chat.

Every run gets an allowlisted environment. No GitHub token reaches an agent;
only the controller publishes. A worker's own model key is the one exception,
because its harness calls the model.
"""

import json
import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
from pydantic import SecretStr

from agent_dispatcher.quota import looks_exhausted, reset_time

# Variables a CLI needs to find its own login, its binaries and a temp directory.
_BASE_ENV = (
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "LANG",
)

Runner = Callable[[list[str], Path, str, Mapping[str, str]], tuple[int, str, str]]


@dataclass(frozen=True)
class RunResult:
    """What a coding run produced; the diff is read from the worktree afterwards."""

    provider: str
    exit_code: int
    output: str
    exhausted: bool
    text: str = ""
    resets_at: datetime | None = None
    harness: str = ""
    model: str | None = None
    effort: str | None = None
    usage: dict[str, int | float] | None = None
    # Why a failed call stopped, when the harness says: "max_turns" or "timed_out".
    stop_reason: str = ""


class TermsError(RuntimeError):
    """The run would break the provider's terms for subscription automation."""


# One model call never runs longer than this; a stuck worker can't hold the queue.
STEP_TIMEOUT_SECONDS = 20 * 60
TIMED_OUT = 124


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill a process and everything it started (shims start the real CLI)."""
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - fixed command, our own child's PID
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover - exercised on the Linux CI runner and VPS
        os.killpg(process.pid, signal.SIGKILL)


def subprocess_runner(
    argv: list[str],
    cwd: Path,
    stdin: str,
    env: Mapping[str, str],
    timeout: float = STEP_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run a CLI with the prompt on stdin, so it never shows up in a process list.

    The program is resolved on PATH first, so Windows npm shims (`claude.cmd`) run.
    On timeout the whole process tree is killed and exit code 124 is returned.
    """
    program = shutil.which(argv[0]) or argv[0]
    # Bytes, not text mode: on Windows text mode rewrites line endings, which
    # breaks patches piped to `git apply`.
    process = subprocess.Popen(  # noqa: S603 - argv is built here, never from input
        [program, *argv[1:]],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A grandchild that survived still holds the pipes; don't wait for it.
            stdout, stderr = b"", b""
        message = f"timed out after {timeout:.0f}s\n".encode()
        return TIMED_OUT, stdout.decode("utf-8", "replace"), message.decode()
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def scrubbed_env(
    source: Mapping[str, str], extra: tuple[str, ...] = ()
) -> dict[str, str]:
    """Copy only allowlisted variables; everything else, tokens included, is dropped.

    The CLIs keep their logins under the user profile. On Windows `HOME` may point
    somewhere else, so it follows `USERPROFILE` when that is set.
    """
    env = {name: source[name] for name in (*_BASE_ENV, *extra) if name in source}
    if "USERPROFILE" in env:
        env["HOME"] = env["USERPROFILE"]
    return env


# Only these built-in tools exist in a run, so the rest aren't sent with every
# turn: thinkers read, workers read and edit. No shell, web, subagents or to-dos.
_TOOLS_READ_ONLY = "Read,Glob,Grep"
_TOOLS_EDITING = "Read,Edit,Write,Glob,Grep"
# Belt and braces: deny the risky ones by name as well.
_DENY_READ_ONLY = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
_DENY_EDITING = "Bash,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
_MODEL_VARIABLES = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)


def _claude_json(stdout: str) -> tuple[str, bool]:
    """Return Claude's result text and whether it reported an error."""
    try:
        result: object = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, True
    if not isinstance(result, dict):
        return "", True
    fields = cast("dict[str, object]", result)
    return str(fields.get("result", "")), bool(fields.get("is_error"))


def _claude_stop(stdout: str) -> str:
    """Return "max_turns" if Claude Code stopped at its turn cap, else ""."""
    try:
        result: object = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(result, dict):
        return ""
    subtype = cast("dict[str, object]", result).get("subtype")
    return "max_turns" if subtype == "error_max_turns" else ""


def _numbers(
    source: Mapping[str, object], names: Mapping[str, str]
) -> dict[str, int | float]:
    """Copy the numeric fields named in `names` (source -> record), skipping others."""
    return {
        record: value
        for field_name, record in names.items()
        if isinstance(value := source.get(field_name), int | float)
        and not isinstance(value, bool)
    }


def _claude_usage(stdout: str) -> dict[str, int | float] | None:
    """Return the tokens and cost Claude Code reported, or None if it reported none."""
    try:
        result: object = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    fields = cast("dict[str, object]", result)
    raw = fields.get("usage")
    usage = _numbers(
        cast("dict[str, object]", raw) if isinstance(raw, dict) else {},
        {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
            "cache_creation_input_tokens": "cache_write_tokens",
        },
    )
    usage |= _numbers(fields, {"total_cost_usd": "reported_cost_usd"})
    return usage or None


def _codex_usage(stdout: str) -> dict[str, int | float] | None:
    """Return the tokens from Codex's last turn that reported usage, if any."""
    usage: dict[str, int | float] | None = None
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = (
            cast("dict[str, object]", event).get("usage")
            if isinstance(event, dict)
            else None
        )
        if isinstance(raw, dict):
            usage = (
                _numbers(
                    cast("dict[str, object]", raw),
                    {
                        "input_tokens": "input_tokens",
                        "cached_input_tokens": "cache_read_tokens",
                        "output_tokens": "output_tokens",
                        "reasoning_output_tokens": "reasoning_tokens",
                    },
                )
                or None
            )
    return usage


def provider_result(  # noqa: PLR0913 - every field of a result comes from the run
    name: str,
    code: int,
    stdout: str,
    text: str,
    error: str,
    *,
    harness: str,
    tuning: "Tuning | None",
    usage: dict[str, int | float] | None,
) -> RunResult:
    """Build a result; only a failed run can be an exhausted one."""
    failed = code != 0
    timed_out = code == TIMED_OUT
    return RunResult(
        provider=name,
        exit_code=code,
        output=stdout,
        exhausted=failed and looks_exhausted(error),
        text=text,
        resets_at=reset_time(error) if failed else None,
        harness=harness,
        model=tuning.model if tuning else None,
        effort=tuning.effort or None if tuning else None,
        usage=usage,
        stop_reason="timed_out" if timed_out else "",
    )


@dataclass(frozen=True)
class Tuning:
    """A model and effort level for one kind of step."""

    model: str
    effort: str


@dataclass(frozen=True)
class Gateway:
    """An Anthropic-compatible endpoint that Claude Code drives instead of Claude.

    MiMo serves one, so MiMo gets Claude Code's file tools without a Claude
    subscription. Its API key does reach the agent process, because the harness
    itself calls the model; no GitHub token ever does.
    """

    base_url: str
    model: str
    api_key: SecretStr

    def env(self) -> dict[str, str]:
        """Return the variables that point Claude Code at this endpoint."""
        return {
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_AUTH_TOKEN": self.api_key.get_secret_value(),
            # Without these Claude Code waits on extended thinking and background
            # calls the gateway doesn't serve.
            "MAX_THINKING_TOKENS": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            **dict.fromkeys(_MODEL_VARIABLES, self.model),
        }


@dataclass(frozen=True)
class ClaudeCli:
    """Claude Code in print mode: Claude on its CLI login, or a model via a gateway."""

    oauth_token: SecretStr | None = None
    gateway: Gateway | None = None
    work: Tuning | None = None
    max_turns: int = 40
    name: str = "claude"

    def argv(
        self,
        *,
        read_only: bool,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> list[str]:
        """Return the command line; the prompt goes on stdin.

        `--setting-sources local` skips user settings (which may route Claude Code
        to another provider) and the repository's own `.claude/settings.json`,
        whose hooks would otherwise run. Without a sandbox there is no Bash: the
        agent edits files, and CI runs the code. Thinking runs can't edit at all.
        `--tools` keeps unused tool definitions out of every turn, and
        `--strict-mcp-config` with no config loads no MCP servers at all.
        """
        argv = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns or self.max_turns),
            "--setting-sources",
            "local",
            "--strict-mcp-config",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            _TOOLS_READ_ONLY if read_only else _TOOLS_EDITING,
            "--disallowedTools",
            _DENY_READ_ONLY if read_only else _DENY_EDITING,
        ]
        tuning = tuning or (None if read_only else self.work)
        if self.gateway is not None:
            argv += ["--model", self.gateway.model]
        elif tuning is not None:
            argv += ["--model", tuning.model, "--effort", tuning.effort]
        return argv

    def run(  # noqa: PLR0913 - the runner and environment are test seams
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one step. On Claude no API key is passed, so it can't bill the API."""
        env = scrubbed_env(environ)
        if self.gateway is not None:
            env.update(self.gateway.env())
        elif self.oauth_token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.oauth_token.get_secret_value()
        argv = self.argv(read_only=read_only, tuning=tuning, max_turns=max_turns)
        code, stdout, stderr = runner(argv, worktree, prompt, env)
        text, is_error = _claude_json(stdout)
        code = code or int(is_error)
        used = tuning or (None if read_only else self.work)
        if self.gateway is not None:
            used = Tuning(self.gateway.model, "")
        result = provider_result(
            self.name,
            code,
            stdout,
            text,
            text if is_error else stderr,
            harness="claude-code",
            tuning=used,
            usage=_claude_usage(stdout),
        )
        stop = _claude_stop(stdout)
        return replace(result, stop_reason=stop) if stop else result


def _codex_events(stdout: str) -> tuple[str, str]:
    """Return the last agent message and any error text from `codex exec --json`."""
    text, errors = "", list[str]()
    for line in stdout.splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        fields = cast("dict[str, object]", event)
        item = fields.get("item")
        if fields.get("type") == "item.completed" and isinstance(item, dict):
            message = cast("dict[str, object]", item)
            if message.get("type") == "agent_message":
                text = str(message.get("text", ""))
        if fields.get("type") in {"error", "turn.failed"}:
            errors.append(line)
    return text, "\n".join(errors)


@dataclass(frozen=True)
class CodexCli:
    """Codex in exec mode, on the ChatGPT-plan login. Never inside GitHub Actions."""

    work: Tuning | None = None
    name: str = "codex"

    def argv(
        self, worktree: Path, *, read_only: bool, tuning: Tuning | None = None
    ) -> list[str]:
        """Return the command line; `-` makes Codex read the prompt from stdin.

        An explicit model and effort keep the dispatcher independent of the
        desktop app's `config.toml`, which would otherwise decide both.
        """
        argv = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only" if read_only else "workspace-write",
            "--cd",
            str(worktree),
        ]
        tuning = tuning or (None if read_only else self.work)
        if tuning is not None:
            argv += ["--model", tuning.model]
            argv += ["--config", f"model_reasoning_effort={tuning.effort}"]
        return [*argv, "-"]

    def run(  # noqa: PLR0913 - the runner and environment are test seams
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one step, refusing to automate the subscription from public CI."""
        if environ.get("GITHUB_ACTIONS") == "true":
            message = "Codex subscription runs are for the controller host only"
            raise TermsError(message)
        del max_turns  # Codex has no turn cap; its own budget applies.
        env = scrubbed_env(environ, ("CODEX_HOME",))
        argv = self.argv(worktree, read_only=read_only, tuning=tuning)
        code, stdout, stderr = runner(argv, worktree, prompt, env)
        text, errors = _codex_events(stdout)
        code = code or int(bool(errors))
        return provider_result(
            self.name,
            code,
            stdout,
            text,
            errors or stderr,
            harness="codex-cli",
            tuning=tuning or (None if read_only else self.work),
            usage=_codex_usage(stdout),
        )


@dataclass(frozen=True)
class OpenCodeCli:
    """OpenCode driving an OpenAI-compatible model, so NVIDIA's GLM can edit files."""

    name: str
    base_url: str
    model: str
    api_key: SecretStr

    def config(self, *, read_only: bool) -> str:
        """Return OpenCode's config: one provider, no shell, no web, no sharing."""
        return json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "share": "disabled",
                "autoupdate": False,
                "provider": {
                    "worker": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": self.name,
                        "options": {
                            "baseURL": self.base_url,
                            "apiKey": "{env:WORKER_API_KEY}",
                        },
                        "models": {self.model: {"name": self.model}},
                    }
                },
                "permission": {
                    "bash": "deny",
                    "webfetch": "deny",
                    "edit": "deny" if read_only else "allow",
                },
            }
        )

    def run(  # noqa: PLR0913 - the runner and environment are test seams
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one step with the prompt in an attached file, never on the command line.

        On Windows, untrusted text in the arguments of a `.cmd` shim can inject
        commands, so the issue text only ever travels in a file.
        """
        del tuning, max_turns  # NVIDIA's model is fixed; OpenCode has no turn cap.
        prompt_file = worktree.parent / f"{worktree.name}.prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        env = scrubbed_env(environ)
        env["OPENCODE_CONFIG_CONTENT"] = self.config(read_only=read_only)
        env["WORKER_API_KEY"] = self.api_key.get_secret_value()
        argv = [
            "opencode",
            "run",
            "--model",
            f"worker/{self.model}",
            "--file",
            str(prompt_file),
            "--",
            "Do the task in the attached file.",
        ]
        code, stdout, stderr = runner(argv, worktree, "", env)
        return provider_result(
            self.name,
            code,
            stdout,
            stdout,
            stderr or stdout,
            harness="opencode",
            tuning=Tuning(self.model, ""),
            usage=None,
        )


@dataclass(frozen=True)
class ChatApi:
    """An OpenAI-compatible chat API, used for triage."""

    name: str
    base_url: str
    model: str
    api_key: SecretStr
    extra_body: Mapping[str, object] | None = None

    def complete(self, system: str, user: str, client: httpx.Client) -> str:
        """Return the reply text. Raises `httpx.HTTPStatusError` on an error status."""
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **(self.extra_body or {}),
        }
        response = client.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"},
            timeout=120,
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])


MIMO_OPENAI = "https://token-plan-ams.xiaomimimo.com/v1"
MIMO_ANTHROPIC = "https://token-plan-ams.xiaomimimo.com/anthropic"
MIMO_MODEL = "mimo-v2.6-flash"
NVIDIA_OPENAI = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "z-ai/glm-5.3-flash"


def mimo(api_key: SecretStr) -> ChatApi:
    """Xiaomi MiMo Flash for chat, with reasoning off (the only switch MiMo honours)."""
    return ChatApi(
        name="mimo",
        base_url=MIMO_OPENAI,
        model=MIMO_MODEL,
        api_key=api_key,
        extra_body={"thinking": {"type": "disabled"}},
    )


def nvidia(api_key: SecretStr) -> ChatApi:
    """NVIDIA's free hosted GLM endpoint for chat."""
    return ChatApi(
        name="nvidia", base_url=NVIDIA_OPENAI, model=NVIDIA_MODEL, api_key=api_key
    )


def mimo_worker(api_key: SecretStr) -> ClaudeCli:
    """MiMo Flash editing files through Claude Code's harness."""
    return ClaudeCli(name="mimo", gateway=Gateway(MIMO_ANTHROPIC, MIMO_MODEL, api_key))


def nvidia_worker(api_key: SecretStr) -> OpenCodeCli:
    """NVIDIA's GLM editing files through OpenCode."""
    return OpenCodeCli(
        name="nvidia", base_url=NVIDIA_OPENAI, model=NVIDIA_MODEL, api_key=api_key
    )
