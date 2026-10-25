"""Provider adapters: the official coding CLIs and two OpenAI-compatible APIs.

Coding runs get an allowlisted environment. No GitHub token, no API key and no
other secret reaches the agent; only the controller publishes.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
from pydantic import SecretStr

from agent_dispatcher.quota import looks_exhausted

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


class TermsError(RuntimeError):
    """The run would break the provider's terms for subscription automation."""


def subprocess_runner(
    argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
) -> tuple[int, str, str]:
    """Run a CLI with the prompt on stdin, so it never shows up in a process list.

    The program is resolved on PATH first, so Windows npm shims (`claude.cmd`) run.
    """
    program = shutil.which(argv[0]) or argv[0]
    # Bytes, not text mode: on Windows text mode rewrites line endings, which
    # breaks patches piped to `git apply`.
    completed = subprocess.run(  # noqa: S603 - argv is built here, never from input
        [program, *argv[1:]],
        cwd=cwd,
        input=stdin.encode("utf-8"),
        env=dict(env),
        capture_output=True,
        check=False,
        timeout=3600,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def scrubbed_env(
    source: Mapping[str, str], extra: tuple[str, ...] = ()
) -> dict[str, str]:
    """Copy only allowlisted variables; everything else, tokens included, is dropped."""
    return {name: source[name] for name in (*_BASE_ENV, *extra) if name in source}


def _claude_error(stdout: str) -> str:
    """Return the error text from Claude's JSON result, or "" if it succeeded."""
    try:
        result: object = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if not isinstance(result, dict):
        return ""
    fields = cast("dict[str, object]", result)
    return str(fields.get("result", "")) if fields.get("is_error") else ""


@dataclass(frozen=True)
class ClaudeCli:
    """Claude Code in print mode, on the subscription token from `setup-token`."""

    oauth_token: SecretStr | None = None
    max_turns: int = 40
    name: str = "claude"

    def argv(self) -> list[str]:
        """Return the command line; the prompt goes on stdin.

        `--setting-sources local` skips user settings (which may route Claude Code
        to another provider) and the repository's own `.claude/settings.json`,
        whose hooks would otherwise run. Without a sandbox there is no Bash: the
        agent edits files, and CI runs the code.
        """
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self.max_turns),
            "--setting-sources",
            "local",
            "--permission-mode",
            "acceptEdits",
            "--disallowedTools",
            "Bash,WebFetch,WebSearch",
        ]

    def run(
        self,
        prompt: str,
        worktree: Path,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one task. No API key is passed, so it can't bill the API."""
        env = scrubbed_env(environ)
        if self.oauth_token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.oauth_token.get_secret_value()
        code, stdout, stderr = runner(self.argv(), worktree, prompt, env)
        error = _claude_error(stdout) or stderr
        return RunResult(self.name, code, stdout, code != 0 and looks_exhausted(error))


@dataclass(frozen=True)
class CodexCli:
    """Codex in exec mode, on the ChatGPT-plan login. Never inside GitHub Actions."""

    name: str = "codex"

    def argv(self, worktree: Path) -> list[str]:
        """Return the command line; `-` makes Codex read the prompt from stdin."""
        return [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(worktree),
            "-",
        ]

    def run(
        self,
        prompt: str,
        worktree: Path,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one task, refusing to automate the subscription from public CI."""
        if environ.get("GITHUB_ACTIONS") == "true":
            message = "Codex subscription runs are for the controller host only"
            raise TermsError(message)
        env = scrubbed_env(environ, ("CODEX_HOME",))
        code, stdout, stderr = runner(self.argv(worktree), worktree, prompt, env)
        return RunResult(self.name, code, stdout, code != 0 and looks_exhausted(stderr))


@dataclass(frozen=True)
class ChatApi:
    """An OpenAI-compatible chat API, used for triage and advisory review only."""

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


def mimo(api_key: SecretStr) -> ChatApi:
    """Xiaomi MiMo Flash with reasoning off, the only switch MiMo honours."""
    return ChatApi(
        name="mimo",
        base_url="https://token-plan-ams.xiaomimimo.com/v1",
        model="mimo-v2.6-flash",
        api_key=api_key,
        extra_body={"thinking": {"type": "disabled"}},
    )


def nvidia(api_key: SecretStr) -> ChatApi:
    """NVIDIA's free hosted GLM endpoint."""
    return ChatApi(
        name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        model="z-ai/glm-5.3-flash",
        api_key=api_key,
    )
