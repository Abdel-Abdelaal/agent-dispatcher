import base64
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from agent_dispatcher import assistant
from agent_dispatcher.assistant import Call, Conversation, Function, handle_input
from agent_dispatcher.assistant_tools import (
    ControllerTools,
    Request,
    ToolError,
    tool_definition,
)
from agent_dispatcher.providers import mimo

PUBLIC = json.dumps(
    {
        "nameWithOwner": "owner/app",
        "isPrivate": False,
        "isArchived": False,
        "isFork": False,
    }
)


@pytest.fixture
def tools(tmp_path: Path) -> ControllerTools:
    return ControllerTools(
        tmp_path,
        "owner",
        Mock(return_value=(0, "{}", "")),
        {"GH_TOKEN": "host-secret"},
        Mock(return_value=True),
    )


def runner(tools: ControllerTools, *outputs: str) -> Mock:
    fake = Mock(side_effect=[(0, output, "") for output in outputs])
    tools.runner = fake
    return fake


def arguments(fake: Mock) -> tuple[list[str], Path, str, dict[str, str]]:
    assert fake.call_args is not None
    return cast("tuple[list[str], Path, str, dict[str, str]]", fake.call_args.args)


def response(text: str = "Done", **fields: object) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": text, **fields}}],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "unknown_text": "must-not-be-retained",
        },
    }


def call(action: str = "status", **fields: object) -> Call:
    return Call(
        id="call1",
        function=Function(
            name="orchestrator", arguments=json.dumps({"action": action, **fields})
        ),
    )


def conversation(
    tools: ControllerTools, *replies: int | dict[str, object]
) -> tuple[Conversation, list[dict[str, Any]]]:
    pending = iter(replies)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        result = next(pending)
        if isinstance(result, int):
            return httpx.Response(result, json={"error": "provider-secret-diagnostic"})
        return httpx.Response(200, json=result)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        Conversation(mimo(SecretStr("mimo-test-credential")), tools, client, Mock()),
        requests,
    )


def test_schema_rejects_unknown_action_and_model_approval() -> None:
    assert tool_definition()[0]["type"] == "function"
    for data in [{"action": "shell"}, {"action": "create_repo", "approved": True}]:
        with pytest.raises(ValidationError):
            Request.model_validate(data)


@pytest.mark.parametrize("value", ["--public", "../app", "other/app", "app;whoami", ""])
def test_owned_name_rejects_shell_options_and_other_owners(
    tools: ControllerTools, value: str
) -> None:
    with pytest.raises(ToolError):
        tools.owned(value)
    assert not cast(Mock, tools.runner).called


def test_owned_accepts_short_and_full_names(tools: ControllerTools) -> None:
    assert tools.owned("app") == tools.owned("owner/app") == "owner/app"


@pytest.mark.parametrize("flag", ["isPrivate", "isArchived", "isFork", "nameWithOwner"])
def test_live_repo_policy_refuses_disallowed_content(
    tools: ControllerTools, flag: str
) -> None:
    row = json.loads(PUBLIC)
    row[flag] = "other/app" if flag == "nameWithOwner" else True
    runner(tools, json.dumps(row))
    with pytest.raises(ToolError):
        tools.public("app")


def test_public_discovery_filters_private_and_inactive_names(
    tools: ControllerTools,
) -> None:
    rows = [
        json.loads(PUBLIC),
        {**json.loads(PUBLIC), "nameWithOwner": "secret", "isPrivate": True},
    ]
    runner(tools, json.dumps(rows))
    result = tools.call(Request(action="list_repos"))
    assert "owner/app" in result
    assert "secret" not in result


@pytest.mark.parametrize("action", ["inspect_repo", "list_work", "inspect_pr"])
def test_public_metadata_tools_are_structured_read_calls(
    tools: ControllerTools, action: str
) -> None:
    fake = runner(tools, PUBLIC, "actual-result")
    assert (
        tools.call(Request.model_validate({"action": action, "repo": "app"}))
        == "actual-result"
    )
    assert arguments(fake)[0][0] == "gh"
    assert "owner/app" in arguments(fake)[0]


def test_search_query_cannot_turn_into_a_cli_flag(tools: ControllerTools) -> None:
    fake = runner(tools, "[]")
    tools.call(Request(action="search_repos", text="--help"))
    assert arguments(fake)[0][-2:] == ["--", "--help"]


def test_read_file_requires_public_repo_and_decodes_content(
    tools: ControllerTools,
) -> None:
    runner(
        tools,
        PUBLIC,
        json.dumps(
            {
                "encoding": "base64",
                "size": 5,
                "content": base64.b64encode(b"hello").decode(),
            }
        ),
    )
    assert tools.call(Request(action="read_file", repo="app")) == "hello"


@pytest.mark.parametrize(
    "path", ["../secret", "/secret", ".env", "x/.env.local", "x\\y", "x?ref=secret"]
)
def test_read_file_rejects_traversal_and_secret_config(
    tools: ControllerTools, path: str
) -> None:
    fake = runner(tools, PUBLIC)
    with pytest.raises(ToolError):
        tools.call(Request(action="read_file", repo="app", path=path))
    assert fake.call_count == 1


@pytest.mark.parametrize(
    "data", [{"encoding": "none"}, {"encoding": "base64", "size": 32001}]
)
def test_large_or_unsupported_files_refused(
    tools: ControllerTools, data: dict[str, object]
) -> None:
    runner(tools, PUBLIC, json.dumps(data))
    with pytest.raises(ToolError):
        tools.call(Request(action="read_file", repo="app"))


def test_create_repo_defaults_private_and_requires_real_approval(
    tools: ControllerTools,
) -> None:
    fake = runner(tools, "owner", "https://github.com/owner/new")
    assert tools.call(Request(action="create_repo", repo="new", text="--public"))
    argv = arguments(fake)[0]
    assert "--private" in argv
    assert "--public" not in argv
    assert "--description=--public" in argv
    assert cast(Mock, tools.approve).call_count == 1


@pytest.mark.parametrize(
    ("login", "approved"), [("wrong-owner", True), ("owner", False)]
)
def test_write_requires_owner_identity_and_terminal_approval(
    tools: ControllerTools, login: str, *, approved: bool
) -> None:
    fake = runner(tools, login)
    tools.approve = Mock(return_value=approved)
    with pytest.raises(ToolError):
        tools.call(Request(action="create_repo", repo="new"))
    assert fake.call_count == 1


def test_queue_public_task_preserves_body_as_stdin(tools: ControllerTools) -> None:
    fake = runner(tools, PUBLIC, "owner", "issue-url")
    body = "Goal: reuse code. Done: tests pass. Literal $(whoami) stays text."
    assert (
        tools.call(Request(action="queue_task", repo="app", title="--help", text=body))
        == "issue-url"
    )
    argv, _, stdin, _ = arguments(fake)
    assert "--title=--help" in argv
    assert body not in argv
    assert argv[-2:] == ["--body-file", "-"]
    assert stdin == body
    assert "agent:ready" in argv


@pytest.mark.parametrize(
    ("repo", "title", "text"),
    [("agent-dispatcher", "fix", "body"), ("app", "", "body"), ("app", "title", "")],
)
def test_queue_cannot_modify_controller_or_publish_empty_task(
    tools: ControllerTools, repo: str, title: str, text: str
) -> None:
    row = json.loads(PUBLIC)
    row["nameWithOwner"] = "owner/" + repo
    fake = runner(tools, json.dumps(row))
    with pytest.raises(ToolError):
        tools.call(Request(action="queue_task", repo=repo, title=title, text=text))
    assert fake.call_count == 1
    assert not cast(Mock, tools.approve).called


def test_remember_is_local_and_loads_with_reviewed_brief(
    tools: ControllerTools,
) -> None:
    runner(tools, "owner", "owner")
    tools.call(Request(action="remember", text="Prefer reuse"))
    tools.call(Request(action="remember", text="Run regression tests"))
    (tools.root / "docs").mkdir()
    (tools.root / "docs" / "assistant-context.md").write_text("Reviewed brief")
    (tools.root / ".env").write_text("never-load-me")
    text = assistant.context(tools.root)
    assert "Reviewed brief" in text
    assert "Prefer reuse" in text
    assert "regression" in text
    assert "never-load-me" not in text


@pytest.mark.parametrize("action", ["status", "start"])
def test_status_and_start_use_controller_commands(
    tools: ControllerTools, action: str
) -> None:
    outputs = ("owner", "started") if action == "start" else ("waiting:0",)
    fake = runner(tools, *outputs)
    assert tools.call(Request.model_validate({"action": action})) == outputs[-1]
    assert arguments(fake)[0][-1] in {"status", "start"}


@pytest.mark.parametrize("action", ["status", "start", "list_repos"])
def test_native_failure_does_not_leak_stderr_or_claim_success(
    tools: ControllerTools, action: str
) -> None:
    outputs = (
        [(0, "owner", ""), (1, "", "secret-diagnostic")]
        if action == "start"
        else [(1, "", "secret-diagnostic")]
    )
    tools.runner = Mock(side_effect=outputs)
    with pytest.raises(ToolError) as error:
        tools.call(Request.model_validate({"action": action}))
    assert "secret-diagnostic" not in str(error.value)


def test_conversation_discovers_repos_then_answers_without_auth_in_model_body(
    tools: ControllerTools,
) -> None:
    runner(tools, "[]")
    tool = call("list_repos").model_dump()
    chat, requests = conversation(
        tools, response("Looking", tool_calls=[tool]), response("No repos")
    )
    assert chat.turn("idk what repo") == "No repos"
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert "host-secret" not in json.dumps(requests)
    assert "mimo-test-credential" not in json.dumps(requests)
    audit = next(
        (tools.root / "runs" / "assistant-telemetry").glob("*.jsonl")
    ).read_text()
    assert "idk what repo" not in audit
    assert "No repos" not in audit
    assert "must-not-be-retained" not in audit
    assert "input_sha256" in audit


@pytest.mark.parametrize(
    "tool",
    [
        Call(id="x", function=Function(name="shell", arguments="{}")),
        call("create_repo", approved=True),
        call("create_repo", repo="new", text="mimo-test-credential"),
        call("read_file", repo="app", path="../bad"),
    ],
)
def test_bad_tools_never_write(tools: ControllerTools, tool: Call) -> None:
    runner(tools, PUBLIC)
    chat, _ = conversation(tools)
    assert "did not complete" in chat.invoke(tool, set())
    assert not cast(Mock, tools.approve).called


def test_duplicate_action_executes_once_per_turn(tools: ControllerTools) -> None:
    fake = runner(tools, "state")
    chat, _ = conversation(tools)
    seen: set[str] = set()
    assert chat.invoke(call(), seen) == "state"
    assert "did not complete" in chat.invoke(call(), seen)
    assert fake.call_count == 1


def test_tool_budget_stops_repeated_requests(tools: ControllerTools) -> None:
    fake = runner(tools, "state")
    reply = response("", tool_calls=[call().model_dump()])
    chat, requests = conversation(tools, *[reply] * 6)
    assert "budget" in chat.turn("work")
    assert len(requests) == 6
    assert fake.call_count == 1
    assert not chat.messages


@pytest.mark.parametrize("failure", [429, 401, {"choices": []}])
def test_provider_failure_clears_incomplete_history_without_tool_action(
    tools: ControllerTools, failure: int | dict[str, object]
) -> None:
    chat, requests = conversation(tools, failure)
    with pytest.raises((httpx.HTTPStatusError, ValidationError)):
        chat.turn("hello")
    assert len(requests) == 1
    assert not chat.messages
    assert not cast(Mock, tools.runner).called


def test_no_text_reply_reports_protocol_problem(tools: ControllerTools) -> None:
    chat, _ = conversation(tools, response(""))
    assert "no text" in chat.turn("hello")
    cast(Mock, chat.output).assert_called_once()


def test_time_budget_prevents_model_call(
    tools: ControllerTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, requests = conversation(tools)
    clock = iter([0, 301])
    monkeypatch.setattr(assistant, "monotonic", lambda: next(clock))
    assert "budget" in chat.turn("hello")
    assert not requests


def test_time_budget_prevents_late_write(
    tools: ControllerTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, _ = conversation(
        tools, response("", tool_calls=[call("create_repo", repo="new").model_dump()])
    )
    clock = iter([0, 1, 301])
    monkeypatch.setattr(assistant, "monotonic", lambda: next(clock))
    assert "budget" in chat.turn("create")
    assert not cast(Mock, tools.runner).called


def test_terminal_commands_and_context_budget(tools: ControllerTools) -> None:
    runner(tools, "waiting:0")
    chat, requests = conversation(tools, response())
    assert handle_input(chat, "/status")
    assert not requests
    assert handle_input(chat, "")
    assert not requests
    assert handle_input(chat, "x" * 12001)
    assert not requests
    chat.messages = [{"role": "user", "content": "x" * 48001}]
    assert handle_input(chat, "hello")
    assert len(requests) == 1
    assert handle_input(chat, "/reset")
    assert not chat.messages
    assert not handle_input(chat, "/quit")


@pytest.mark.parametrize("failure", [429, 401, {"choices": []}])
def test_terminal_reports_failures_and_accepts_quit(
    tools: ControllerTools,
    monkeypatch: pytest.MonkeyPatch,
    failure: int | dict[str, object],
) -> None:
    chat, _ = conversation(tools, failure)
    inputs = iter(["hello", "/quit"])
    monkeypatch.setattr("builtins.input", Mock(side_effect=inputs))
    assert assistant.interact(chat) == 0
    assert "provider-secret-diagnostic" not in str(
        cast(Mock, chat.output).call_args_list
    )


def test_terminal_eof_closes_without_stopping_dispatcher(
    tools: ControllerTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, _ = conversation(tools)
    monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
    assert assistant.interact(chat) == 0
    assert not cast(Mock, tools.runner).called


def test_main_requires_configured_mimo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        assistant, "Settings", Mock(return_value=Mock(mimo_api_key=None))
    )
    assert assistant.main() == 1


def test_main_wires_existing_model_and_controller_without_live_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assistant,
        "Settings",
        Mock(return_value=Mock(mimo_api_key=SecretStr("synthetic"), owner="owner")),
    )
    monkeypatch.setattr("builtins.input", Mock(return_value="/quit"))
    assert assistant.main() == 0
