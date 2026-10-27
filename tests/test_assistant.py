import base64
import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from agent_dispatcher.assistant_context import context
from agent_dispatcher.assistant_tools import ControllerTools, Request, ToolError

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


def test_schema_rejects_unknown_action_and_model_approval() -> None:
    assert Request.model_json_schema()["additionalProperties"] is False
    for data in [{"action": "shell"}, {"action": "create_repo", "approved": True}]:
        with pytest.raises(ValidationError):
            Request.model_validate(data)


def test_run_evidence_requires_public_repo_and_broker_scoped_lookup(
    tools: ControllerTools,
) -> None:
    fake = runner(tools, PUBLIC, '{"calls":[]}')
    assert (
        tools.call(Request(action="inspect_run", repo="app", number=28))
        == '{"calls":[]}'
    )
    argv, _, _, _ = arguments(fake)
    assert argv[1:] == ["evidence", "28", "--repo", "owner/app"]
    runner(tools, json.dumps({**json.loads(PUBLIC), "isPrivate": True}))
    with pytest.raises(ToolError):
        tools.call(Request(action="inspect_run", repo="app", number=28))


def test_run_evidence_failure_never_returns_native_diagnostics(
    tools: ControllerTools,
) -> None:
    fake = Mock(side_effect=[(0, PUBLIC, ""), (1, "private-payload", "host-secret")])
    tools.runner = fake
    with pytest.raises(ToolError, match="Could not read"):
        tools.call(Request(action="inspect_run", repo="app", number=28))


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
    text = context(tools.root)
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
