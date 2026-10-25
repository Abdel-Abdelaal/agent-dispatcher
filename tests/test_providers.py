import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agent_dispatcher.providers import (
    ClaudeCli,
    CodexCli,
    TermsError,
    mimo,
    nvidia,
    scrubbed_env,
    subprocess_runner,
)

HOST_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/agent",
    "GITHUB_TOKEN": "ghs_should_not_pass",
    "GH_TOKEN": "gho_should_not_pass",
    "ANTHROPIC_API_KEY": "sk-ant-should-not-pass",
    "DISPATCHER_MIMO_API_KEY": "should-not-pass",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-for-claude-only",
}


class FakeRunner:
    def __init__(self, code: int = 0, stdout: str = "{}", stderr: str = "") -> None:
        self.result = (code, stdout, stderr)
        self.calls: list[tuple[list[str], Path, str, Mapping[str, str]]] = []

    def __call__(
        self, argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        self.calls.append((argv, cwd, stdin, env))
        return self.result


def test_scrubbed_env_keeps_only_the_allowlist() -> None:
    assert scrubbed_env(HOST_ENV) == {"PATH": "/usr/bin", "HOME": "/home/agent"}


def test_home_follows_the_windows_user_profile() -> None:
    env = scrubbed_env({"HOME": "C:/Other", "USERPROFILE": "C:/Users/a"})
    assert env["HOME"] == "C:/Users/a"


def test_claude_gets_its_token_but_no_other_secret(tmp_path: Path) -> None:
    runner = FakeRunner()
    claude = ClaudeCli(oauth_token=SecretStr("oauth-token"))
    result = claude.run("do it", tmp_path, runner, HOST_ENV)
    argv, cwd, stdin, env = runner.calls[0]
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--setting-sources") + 1] == "local"
    assert "Bash,WebFetch,WebSearch" in argv
    assert (cwd, stdin) == (tmp_path, "do it")
    assert set(env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "oauth-token" not in repr(claude)
    assert result.exit_code == 0
    assert not result.exhausted


def test_claude_without_a_token_passes_none(tmp_path: Path) -> None:
    runner = FakeRunner()
    ClaudeCli().run("x", tmp_path, runner, HOST_ENV)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in runner.calls[0][3]


def test_claude_exhaustion_is_read_from_its_json_error(tmp_path: Path) -> None:
    limit = json.dumps({"is_error": True, "result": "Claude usage limit reached"})
    assert ClaudeCli().run("x", tmp_path, FakeRunner(1, limit), HOST_ENV).exhausted
    other = json.dumps({"is_error": True, "result": "Failed to authenticate"})
    assert not ClaudeCli().run("x", tmp_path, FakeRunner(1, other), HOST_ENV).exhausted


def test_claude_success_text_never_counts_as_exhaustion(tmp_path: Path) -> None:
    done = json.dumps({"is_error": False, "result": "handled the rate limit case"})
    result = ClaudeCli().run("x", tmp_path, FakeRunner(0, done), HOST_ENV)
    assert not result.exhausted


def test_claude_non_json_output_is_treated_as_the_error(tmp_path: Path) -> None:
    runner = FakeRunner(1, "usage limit reached", "")
    assert ClaudeCli().run("x", tmp_path, runner, HOST_ENV).exhausted
    assert (
        not ClaudeCli().run("x", tmp_path, FakeRunner(1, "[1, 2]"), HOST_ENV).exhausted
    )


def test_codex_runs_sandboxed_with_prompt_on_stdin(tmp_path: Path) -> None:
    runner = FakeRunner()
    CodexCli().run("do it", tmp_path, runner, HOST_ENV)
    argv, _, stdin, env = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "-"
    assert stdin == "do it"
    assert "GITHUB_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_codex_refuses_to_run_in_github_actions(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(TermsError):
        CodexCli().run("x", tmp_path, runner, {**HOST_ENV, "GITHUB_ACTIONS": "true"})
    assert runner.calls == []


def test_subprocess_runner_passes_stdin_and_env(tmp_path: Path) -> None:
    script = "import os, sys; print(sys.stdin.read() + os.environ['MARK'])"
    code, stdout, _ = subprocess_runner(
        ["python", "-c", script],
        tmp_path,
        "hi ",
        {**scrubbed_env(HOST_ENV), "MARK": "x"},
    )
    assert (code, stdout.strip()) == (0, "hi x")


def chat_client(seen: list[httpx.Request], status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = {"choices": [{"message": {"content": "triaged"}}]}
        return httpx.Response(status, json=reply)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_mimo_turns_reasoning_off_and_sends_the_key() -> None:
    seen: list[httpx.Request] = []
    reply = mimo(SecretStr("k1")).complete("sys", "user", chat_client(seen))
    body = json.loads(seen[0].content)
    assert reply == "triaged"
    assert body["model"] == "mimo-v2.6-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert seen[0].headers["Authorization"] == "Bearer k1"


def test_nvidia_uses_glm_and_raises_on_errors() -> None:
    seen: list[httpx.Request] = []
    provider = nvidia(SecretStr("k2"))
    assert "k2" not in repr(provider)
    with pytest.raises(httpx.HTTPStatusError):
        provider.complete("s", "u", chat_client(seen, status=401))
    assert json.loads(seen[0].content)["model"] == "z-ai/glm-5.3-flash"
