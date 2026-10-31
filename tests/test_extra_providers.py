"""Any OpenAI-compatible endpoint joins the team through configuration alone."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from agent_dispatcher import cli
from agent_dispatcher.dispatcher import choose_team
from agent_dispatcher.opencode_worker import OpenCodeWorker
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.quota import Quota
from agent_dispatcher.roles import Team
from agent_dispatcher.settings import ExtraProvider, Settings

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
OPENROUTER = {
    "name": "openrouter",
    "base_url": "https://openrouter.ai/api/v1/",
    "model": "deepseek/deepseek-chat",
    "api_key": "or-test-only",
    "context": 64000,
    "output": 8000,
}
LOCAL = {
    "name": "ollama",
    "base_url": "http://localhost:11434/v1",
    "model": "qwen3-coder",
    "api_key": "unused",
    "role": "thinker",
}


def settings(*extras: Mapping[str, object], **values: object) -> Settings:
    return Settings.model_validate({"extra_providers": list(extras), **values})


def test_extra_providers_come_from_one_json_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPATCHER_EXTRA_PROVIDERS", json.dumps([OPENROUTER, LOCAL]))
    loaded = Settings().extra_providers
    assert [extra.name for extra in loaded] == ["openrouter", "ollama"]
    assert loaded[0].base_url == "https://openrouter.ai/api/v1"
    assert loaded[0].role == "worker"
    assert loaded[1].role == "thinker"
    assert loaded[1].context == 128_000
    assert "or-test-only" not in repr(loaded)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "mimo"},
        {"name": "Has Spaces"},
        {"base_url": "http://openrouter.ai/api/v1"},
        {"base_url": "http://localhost.example.com/v1"},
        {"base_url": "ftp://localhost/v1"},
        {"base_url": "openrouter.ai"},
        {"model": ""},
        {"role": "boss"},
        {"context": 0},
        {"temperature": 2},
    ],
)
def test_unsafe_or_ambiguous_entries_are_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExtraProvider.model_validate({**OPENROUTER, **change})


@pytest.mark.parametrize(
    "url", ["http://localhost:1234/v1", "http://127.0.0.1:8000/v1", "http://[::1]/v1"]
)
def test_plain_http_is_allowed_only_to_this_machine(url: str) -> None:
    assert ExtraProvider.model_validate({**LOCAL, "base_url": url}).base_url == url


def test_extra_provider_names_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        settings(OPENROUTER, {**LOCAL, "name": "openrouter"})


def test_extras_are_configured_unless_disabled_and_their_keys_are_scrubbed() -> None:
    both = settings(OPENROUTER, LOCAL)
    assert cli.configured(both)[-2:] == ["openrouter", "ollama"]
    assert {"or-test-only", "unused"} <= set(cli.held_secrets(both))
    off = settings(OPENROUTER, LOCAL, disabled_providers="ollama")
    assert "ollama" not in cli.configured(off)
    assert "ollama" not in cli.providers(off)


def test_extras_follow_the_built_in_providers_of_their_role() -> None:
    roster = cli.roster(settings(OPENROUTER, LOCAL))
    assert roster.thinkers == ("claude", "codex", "ollama")
    assert roster.workers == ("mimo", "nvidia", "openrouter")
    everyone = {
        name: Quota(name, 0.1, None)
        for name in ("claude", "codex", "mimo", "openrouter", "ollama")
    }
    team = choose_team(everyone, 0.6, NOW, roster=roster)
    assert team == Team(planner="claude", worker="mimo", reviewer="codex")
    # With the built-in providers out, the extras form the team on their own.
    extras_only = {name: everyone[name] for name in ("openrouter", "ollama")}
    team = choose_team(extras_only, 0.6, NOW, roster=roster)
    assert team == Team(planner="ollama", worker="openrouter", reviewer="ollama")


def test_one_extra_worker_can_do_everything_like_mimo_only() -> None:
    roster = cli.roster(settings(OPENROUTER))
    quotas = {"openrouter": Quota("openrouter", 0.1, None)}
    assert choose_team(quotas, 0.6, NOW, roster=roster) is None
    team = choose_team(quotas, 0.6, NOW, roster=roster, allow_worker_thinking=True)
    assert team is not None
    assert team.members() == ("openrouter",)


def test_an_extra_runs_through_the_same_locked_down_opencode_harness() -> None:
    adapter = cli.providers(settings(OPENROUTER, opencode_executable="D:/oc.exe"))[
        "openrouter"
    ]
    assert isinstance(adapter, OpenCodeWorker)
    assert adapter.executable == "D:/oc.exe"
    assert adapter.checkpoint_root is not None
    config = json.loads(adapter.config(read_only=False, steps=5))
    worker = config["provider"]["worker"]
    assert worker["npm"] == "@ai-sdk/openai-compatible"
    assert worker["options"]["baseURL"] == "https://openrouter.ai/api/v1"
    assert worker["options"]["apiKey"] == "{env:WORKER_API_KEY}"
    assert worker["models"] == {
        "deepseek/deepseek-chat": {
            "name": "deepseek/deepseek-chat",
            "limit": {"context": 64000, "output": 8000},
        }
    }
    assert config["model"] == config["small_model"] == "worker/deepseek/deepseek-chat"
    assert config["enabled_providers"] == ["worker"]
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["read"]["*.env"] == "deny"
    assert config["mcp"] == {}
    assert "or-test-only" not in json.dumps(config)


def test_an_extra_run_sends_its_key_by_environment_and_names_its_model(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    def runner(
        argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        del cwd, stdin
        seen["model"] = argv[argv.index("--model") + 1]
        seen["key"] = env["WORKER_API_KEY"]
        seen["argv"] = " ".join(argv)
        return 0, json.dumps({"type": "text", "part": {"text": "done"}}), ""

    adapter = OpenCodeWorker(
        SecretStr("or-test-only"),
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-chat",
        model_settings={"name": "deepseek/deepseek-chat"},
    )
    result = adapter.run("task", tmp_path, runner=runner, environ={"PATH": "x"})
    assert seen["model"] == "worker/deepseek/deepseek-chat"
    assert seen["key"] == "or-test-only"
    assert "or-test-only" not in seen["argv"]
    assert result.provider == "openrouter"
    assert result.model == "deepseek/deepseek-chat"
    assert result.text == "done"


def test_an_exhausted_extra_is_parked_like_any_provider(tmp_path: Path) -> None:
    def runner(
        argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        del argv, cwd, stdin, env
        return 1, "", "429 Too Many Requests: rate limit exceeded"

    adapter = OpenCodeWorker(SecretStr("k"), name="openrouter")
    assert adapter.run("task", tmp_path, runner=runner, environ={}).exhausted


def test_routing_records_name_every_extra_left_out(tmp_path: Path) -> None:
    config = settings(OPENROUTER, LOCAL, work_root=tmp_path)
    queue = RunQueue(tmp_path / "q.sqlite3")
    team = Team(planner="claude", worker="mimo", reviewer="codex")
    cli.record_routing(config, queue, team, 1, Task("o/r", 1, "title"))
    lines = (tmp_path / "telemetry").rglob("*.jsonl")
    providers = {
        json.loads(line)["provider"]
        for path in lines
        for line in path.read_text(encoding="utf-8").splitlines()
    }
    assert {"nvidia", "openrouter", "ollama"} <= providers
