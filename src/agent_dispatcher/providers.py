"""Provider adapters: the official coding CLIs and two OpenAI-compatible APIs.

Coding runs get an allowlisted environment. No GitHub token, no API key and no
other secret reaches the agent; only the controller publishes.
"""

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

from agent_dispatcher.quota import looks_exhausted

# Variables a CLI needs to find its own login, its binaries and a temp directory.
_BASE_ENV = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG")

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
    """Run a CLI with the prompt on stdin, so it never shows up in a process list."""
    completed = subprocess.run(  # noqa: S603 - argv is built here, never from input
        argv,
        cwd=cwd,
        input=stdin,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=3600,
    )
    return completed.returncode, completed.stdout, completed.stderr


def scrubbed_env(
    source: Mapping[str, str], extra: tuple[str, ...] = ()
) -> dict[str, str]:
    """Copy only allowlisted variables; everything else, tokens included, is dropped."""
    return {name: source[name] for name in (*_BASE_ENV, *extra) if name in source}


@dataclass(frozen=True)
class ClaudeCli:
    """Claude Code in print mode, on the subscription login from `setup-token`."""

    max_turns: int = 40
    name: str = "claude"

    def argv(self) -> list[str]:
        """Return the command line; the prompt goes on stdin."""
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            "acceptEdits",
            "--disallowedTools",
            "WebFetch,WebSearch",
        ]

    def run(
        self,
        prompt: str,
        worktree: Path,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] = os.environ,
    ) -> RunResult:
        """Run one task. The API key is never passed, so it can't bill the API."""
        env = scrubbed_env(environ, ("CLAUDE_CODE_OAUTH_TOKEN",))
        code, stdout, stderr = runner(self.argv(), worktree, prompt, env)
        return RunResult(self.name, code, stdout, code != 0 and looks_exhausted(stderr))


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
