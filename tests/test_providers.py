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
    Tuning,
    mimo,
    mimo_worker,
    nvidia,
    nvidia_worker,
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
    result = claude.run("do it", tmp_path, runner=runner, environ=HOST_ENV)
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
    ClaudeCli().run("x", tmp_path, runner=runner, environ=HOST_ENV)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in runner.calls[0][3]


def test_claude_exhaustion_is_read_from_its_json_error(tmp_path: Path) -> None:
    limit = json.dumps({"is_error": True, "result": "Claude usage limit reached"})
    assert (
        ClaudeCli()
        .run("x", tmp_path, runner=FakeRunner(1, limit), environ=HOST_ENV)
        .exhausted
    )
    other = json.dumps({"is_error": True, "result": "Failed to authenticate"})
    assert (
        not ClaudeCli()
        .run("x", tmp_path, runner=FakeRunner(1, other), environ=HOST_ENV)
        .exhausted
    )


def test_claude_success_text_never_counts_as_exhaustion(tmp_path: Path) -> None:
    done = json.dumps({"is_error": False, "result": "handled the rate limit case"})
    result = ClaudeCli().run(
        "x", tmp_path, runner=FakeRunner(0, done), environ=HOST_ENV
    )
    assert not result.exhausted


def test_claude_non_json_output_is_treated_as_the_error(tmp_path: Path) -> None:
    runner = FakeRunner(1, "usage limit reached", "")
    assert ClaudeCli().run("x", tmp_path, runner=runner, environ=HOST_ENV).exhausted
    assert (
        not ClaudeCli()
        .run("x", tmp_path, runner=FakeRunner(1, "[1, 2]"), environ=HOST_ENV)
        .exhausted
    )


def test_codex_runs_sandboxed_with_prompt_on_stdin(tmp_path: Path) -> None:
    runner = FakeRunner()
    CodexCli().run("do it", tmp_path, runner=runner, environ=HOST_ENV)
    argv, _, stdin, env = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "-"
    assert stdin == "do it"
    assert "GITHUB_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_codex_refuses_to_run_in_github_actions(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(TermsError):
        CodexCli().run(
            "x", tmp_path, runner=runner, environ={**HOST_ENV, "GITHUB_ACTIONS": "true"}
        )
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


def test_claude_thinking_runs_cannot_edit(tmp_path: Path) -> None:
    runner = FakeRunner(0, json.dumps({"is_error": False, "result": "1. plan"}))
    result = ClaudeCli().run(
        "p", tmp_path, read_only=True, runner=runner, environ=HOST_ENV
    )
    argv = runner.calls[0][0]
    assert argv[argv.index("--disallowedTools") + 1].startswith("Bash,Edit,Write")
    assert result.text == "1. plan"


def test_claude_error_with_exit_zero_still_fails(tmp_path: Path) -> None:
    runner = FakeRunner(0, json.dumps({"is_error": True, "result": "boom"}))
    assert (
        ClaudeCli().run("x", tmp_path, runner=runner, environ=HOST_ENV).exit_code == 1
    )


def test_mimo_worker_drives_claude_code_through_its_gateway(tmp_path: Path) -> None:
    runner = FakeRunner()
    worker = mimo_worker(SecretStr("mimo-key"))
    worker.run("do it", tmp_path, runner=runner, environ=HOST_ENV)
    argv, _, _, env = runner.calls[0]
    assert worker.name == "mimo"
    assert argv[argv.index("--model") + 1] == "mimo-v2.6-flash"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "mimo-key"
    assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_codex_reads_its_last_message_and_reset_time(tmp_path: Path) -> None:
    events = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            "not json",
            json.dumps([1]),
            json.dumps({"type": "item.completed", "item": {"type": "reasoning"}}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ok"},
                }
            ),
        ]
    )
    done = CodexCli().run(
        "x", tmp_path, read_only=True, runner=FakeRunner(0, events), environ=HOST_ENV
    )
    assert (done.text, done.exit_code, done.exhausted) == ("ok", 0, False)
    limit = json.dumps(
        {
            "type": "turn.failed",
            "error": {
                "message": "You've hit your usage limit. "
                "Try again at Oct 26th, 2026 1:27 AM."
            },
        }
    ).replace("Try again", "try again")
    spent = CodexCli().run("x", tmp_path, runner=FakeRunner(0, limit), environ=HOST_ENV)
    assert spent.exhausted
    assert spent.exit_code == 1
    assert spent.resets_at is not None
    assert (spent.resets_at.month, spent.resets_at.day) == (10, 26)


def test_codex_thinking_runs_use_a_read_only_sandbox(tmp_path: Path) -> None:
    runner = FakeRunner()
    CodexCli().run("x", tmp_path, read_only=True, runner=runner, environ=HOST_ENV)
    argv = runner.calls[0][0]
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_nvidia_worker_keeps_the_prompt_off_the_command_line(tmp_path: Path) -> None:
    worktree = tmp_path / "run-1-agent"
    worktree.mkdir()
    runner = FakeRunner(0, "done")
    worker = nvidia_worker(SecretStr("nv-key"))
    result = worker.run(
        "SECRET-ISSUE-TEXT & calc", worktree, runner=runner, environ=HOST_ENV
    )
    argv, cwd, stdin, env = runner.calls[0]
    assert not any("SECRET-ISSUE-TEXT" in arg for arg in argv)
    assert stdin == ""
    assert cwd == worktree
    prompt_file = Path(argv[argv.index("--file") + 1])
    assert prompt_file.parent == tmp_path
    assert prompt_file.read_text(encoding="utf-8") == "SECRET-ISSUE-TEXT & calc"
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"] == {"bash": "deny", "webfetch": "deny", "edit": "allow"}
    assert config["provider"]["worker"]["options"]["apiKey"] == "{env:WORKER_API_KEY}"
    assert env["WORKER_API_KEY"] == "nv-key"
    assert "nv-key" not in env["OPENCODE_CONFIG_CONTENT"]
    assert argv[argv.index("--model") + 1] == "worker/z-ai/glm-5.3-flash"
    assert (result.text, result.exhausted) == ("done", False)
    read_only = json.loads(worker.config(read_only=True))
    assert read_only["permission"]["edit"] == "deny"


def test_nvidia_rate_limit_parks_the_worker(tmp_path: Path) -> None:
    worktree = tmp_path / "w"
    worktree.mkdir()
    runner = FakeRunner(1, "", "Error: 429 Too Many Requests")
    assert (
        nvidia_worker(SecretStr("k"))
        .run("x", worktree, runner=runner, environ=HOST_ENV)
        .exhausted
    )


def test_thinking_and_working_use_their_own_model_and_effort(tmp_path: Path) -> None:
    claude = ClaudeCli(
        think=Tuning("claude-opus-5-5", "high"),
        work=Tuning("claude-sonnet-5-5", "medium"),
    )
    think = claude.argv(read_only=True)
    work = claude.argv(read_only=False)
    assert think[think.index("--model") + 1 :][:3] == [
        "claude-opus-5-5",
        "--effort",
        "high",
    ]
    assert work[work.index("--model") + 1 :][:3] == [
        "claude-sonnet-5-5",
        "--effort",
        "medium",
    ]
    assert "--model" not in ClaudeCli().argv(read_only=True)
    codex = CodexCli(
        think=Tuning("gpt-6.1-sol", "high"), work=Tuning("gpt-6.1-sol", "medium")
    )
    argv = codex.argv(tmp_path, read_only=True)
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("--config") + 1] == "model_reasoning_effort=high"
    assert argv[-1] == "-"
    assert "model_reasoning_effort=medium" in codex.argv(tmp_path, read_only=False)
    assert "--model" not in CodexCli().argv(tmp_path, read_only=True)
