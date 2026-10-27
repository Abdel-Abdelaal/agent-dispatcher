import io
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from agent_dispatcher import fleet_mcp, native_ui
from agent_dispatcher.assistant_tools import ControllerTools
from agent_dispatcher.fleet_mcp import WRITES, Bridge, definitions, serve
from agent_dispatcher.settings import Settings, load_settings


@pytest.fixture
def bridge(tmp_path: Path) -> Bridge:
    tools = ControllerTools(
        tmp_path,
        "owner",
        Mock(return_value=(0, "waiting:0", "")),
        {},
        Mock(return_value=True),
    )
    return Bridge(tools)


def test_native_tools_have_separate_write_names_and_forbid_model_action_override() -> (
    None
):
    names = {tool["name"] for tool in definitions()}
    assert names >= WRITES
    for tool in definitions():
        schema = cast("dict[str, Any]", tool["inputSchema"])
        assert "action" not in schema["properties"]
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("method", ["tools/list", "tools/call", "unknown"])
def test_mcp_requires_initialization(bridge: Bridge, method: str) -> None:
    result = bridge.reply({"id": 1, "method": method})
    assert result is not None
    assert "error" in result


def test_mcp_handshake_ping_discovery_and_notifications(bridge: Bridge) -> None:
    assert bridge.reply({"method": "notifications/initialized"}) is None
    assert bridge.reply({"id": 1, "method": "initialize"})
    assert bridge.reply({"id": 2, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    result = bridge.reply({"id": 3, "method": "tools/list"})
    assert result is not None
    assert "result" in result
    assert bridge.reply({"id": 4, "method": "unknown"})
    reply = bridge.reply(
        {"id": 5, "method": "tools/call", "params": {"name": "status"}}
    )
    assert reply is not None
    assert "waiting:0" in json.dumps(reply)


@pytest.mark.parametrize("action", sorted(WRITES))
def test_mcp_writes_fail_closed_without_native_launch(
    bridge: Bridge, action: str
) -> None:
    result = bridge.execute({"name": action, "arguments": {"repo": "new"}})
    assert result["isError"] is True
    assert not cast(Mock, bridge.tools.runner).called


@pytest.mark.parametrize(
    "params",
    [
        {"name": "shell"},
        {"name": "status", "arguments": []},
        {"name": "status", "arguments": {"action": "start"}},
        {"name": "status", "arguments": {"approved": True}},
        {"name": "status", "arguments": {"text": "ghp_" + "a" * 30}},
    ],
)
def test_mcp_rejects_unknown_injected_or_secret_arguments(
    bridge: Bridge, params: dict[str, Any]
) -> None:
    assert bridge.execute(params)["isError"] is True
    assert not cast(Mock, bridge.tools.runner).called


def test_native_ui_approval_precedes_broker_write_and_success_is_deduplicated(
    bridge: Bridge,
) -> None:
    bridge.writes_enabled = True
    fake = Mock(side_effect=[(0, "owner", ""), (0, "created", "")])
    bridge.tools.runner = fake
    request = {"name": "create_repo", "arguments": {"repo": "new"}}
    first = bridge.execute(request)
    assert "isError" not in first
    assert bridge.execute(request) == first
    assert fake.call_count == 2


def test_failed_write_is_not_automatically_retried(bridge: Bridge) -> None:
    bridge.writes_enabled = True
    fake = Mock(side_effect=[(0, "owner", ""), (1, "", "secret-diagnostic")])
    bridge.tools.runner = fake
    request = {"name": "create_repo", "arguments": {"repo": "new"}}
    first = bridge.execute(request)
    assert first["isError"] is True
    assert bridge.execute(request) == first
    assert fake.call_count == 2
    assert "secret-diagnostic" not in json.dumps(first)


@pytest.mark.parametrize(
    "text", ["[]\n", "not json\n", "x" * 64001], ids=["array", "invalid", "oversized"]
)
def test_mcp_transport_refuses_invalid_or_oversized_frames(
    bridge: Bridge, text: str
) -> None:
    assert serve(bridge, io.StringIO(text), io.StringIO()) == 1


def test_mcp_stdout_contains_only_protocol_json(bridge: Bridge) -> None:
    frames = [
        {"id": 1, "method": "initialize"},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/call", "params": {"name": "status"}},
    ]
    output = io.StringIO()
    assert serve(bridge, io.StringIO("\n".join(map(json.dumps, frames))), output) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [1, 2]


def test_native_config_preserves_permissions_context_and_only_mimo(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "assistant-context.md").write_text("Reviewed rules")
    config = native_ui.configuration(tmp_path, Path("python.exe"))
    assert config["enabled_providers"] == ["worker"]
    assert config["default_agent"] == "trifecta"
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["trifecta_*"] == "allow"
    for name in WRITES:
        assert config["permission"]["trifecta_" + name] == "ask"
    assert config["agent"]["trifecta"]["permission"] == config["permission"]
    assert "Reviewed rules" in config["agent"]["trifecta"]["prompt"]
    assert config["share"] == "disabled"
    assert config["compaction"]["auto"] is True
    assert "{env:WORKER_API_KEY}" in json.dumps(config)
    assert "--auto" not in json.dumps(config)


def test_read_only_native_probe_denies_writes_in_client_and_server(
    tmp_path: Path,
) -> None:
    config = native_ui.configuration(tmp_path, Path("python.exe"), read_only=True)
    for name in WRITES:
        assert config["permission"]["trifecta_" + name] == "deny"
    assert "--native-ui" not in config["mcp"]["trifecta"]["command"]


@pytest.mark.parametrize("reasoning", ["enabled", "disabled"])
def test_native_review_respects_reasoning_setting_and_worker_context_budget(
    tmp_path: Path, reasoning: str
) -> None:
    settings = Settings.model_validate({"mimo_reasoning": reasoning})
    env = native_ui.environment(tmp_path, settings)
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    model = config["provider"]["worker"]["models"]["mimo-v2.6-flash"]
    assert model["options"]["thinking"]["type"] == reasoning
    assert model["interleaved"] == {"field": "reasoning_content"}
    assert model["limit"] == {"context": 196608, "output": 32000}


def test_native_environment_has_no_other_provider_or_github_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider")
    settings = Settings(mimo_api_key=SecretStr("mimo-key"))
    env = native_ui.environment(tmp_path, settings)
    assert "GH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["WORKER_API_KEY"] == "mimo-key"
    assert "mimo-key" not in env["OPENCODE_CONFIG_CONTENT"]
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_TEST_HOME"].startswith(str(tmp_path))
    assert env["XDG_DATA_HOME"] == str(tmp_path / "runs/native-opencode/data")


def test_controller_reuses_owner_login_without_copying_host_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("DISPATCHER_OWNER=owner\n")
    tools = fleet_mcp.controller(
        tmp_path, {"PATH": "bin", "GH_TOKEN": "secret", "WORKER_API_KEY": "model"}
    )
    assert tools.owner == "owner"
    assert tools.environ == {"PATH": "bin"}
    assert tools.approve("native UI handled this") is True
    assert load_settings(tmp_path / ".env").owner == "owner"


def test_native_launcher_refuses_missing_key_or_unreviewed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_ui, "load_settings", Mock(return_value=Settings()))
    assert native_ui.main() == 1
    monkeypatch.setattr(
        native_ui,
        "load_settings",
        Mock(
            return_value=Settings(
                mimo_api_key=SecretStr("key"), opencode_executable="opencode"
            )
        ),
    )
    assert native_ui.main() == 1


def test_native_launcher_starts_real_interface_without_auto_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "opencode.exe"
    binary.touch()
    settings = Settings(mimo_api_key=SecretStr("key"), opencode_executable=str(binary))
    monkeypatch.setattr(native_ui, "load_settings", Mock(return_value=settings))
    launch = Mock(return_value=0)
    monkeypatch.setattr(native_ui.subprocess, "call", launch)
    monkeypatch.setattr(
        native_ui, "resume_args", Mock(return_value=["--session", "ses_saved"])
    )
    assert native_ui.main() == 0
    argv = launch.call_args.args[0]
    assert "--session" in argv
    assert "--pure" in argv
    assert "--auto" not in argv
    assert "key" not in argv


@pytest.mark.parametrize(
    "output", ["[]", "bad-json", "{}", '[{"id":"dummy"}]', "[null]"]
)
def test_first_launch_does_not_resume_invalid_or_absent_session(
    tmp_path: Path,
    output: str,
) -> None:
    assert (
        native_ui.resume_args(
            "opencode", tmp_path, {}, Mock(return_value=(0, output, ""))
        )
        == []
    )


def test_resume_uses_valid_saved_session_and_handles_failed_listing(
    tmp_path: Path,
) -> None:
    assert native_ui.resume_args(
        "opencode", tmp_path, {}, Mock(return_value=(0, '[{"id":"ses_saved123"}]', ""))
    ) == ["--session", "ses_saved123"]
    assert (
        native_ui.resume_args(
            "opencode", tmp_path, {}, Mock(return_value=(1, "", "error"))
        )
        == []
    )


def test_mcp_main_routes_only_protocol_through_stdio(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet_mcp, "controller", Mock(return_value=bridge.tools))
    monkeypatch.setattr(
        fleet_mcp.sys, "stdin", io.StringIO('{"id":1,"method":"initialize"}\n')
    )
    monkeypatch.setattr(fleet_mcp.sys, "stdout", io.StringIO())
    assert fleet_mcp.main() == 0
