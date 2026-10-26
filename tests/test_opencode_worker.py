"""The alternative harness keeps limits, credentials and retained state bounded."""

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import SecretStr

from agent_dispatcher import cli
from agent_dispatcher.opencode_worker import MiMoOpenCode, events
from agent_dispatcher.settings import Settings


def test_event_parser_keeps_final_reply_and_known_usage() -> None:
    output = "\n".join(
        [
            "noise",
            "[]",
            json.dumps({"type": "text", "part": {"text": "working"}}),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 12,
                            "output": 5,
                            "reasoning": -1,
                        }
                    },
                }
            ),
            json.dumps({"type": "text", "part": {"text": "final"}}),
        ]
    )
    assert events(output) == ("final", {"input_tokens": 12, "output_tokens": 5}, False)
    assert events('{"type":"error"}') == ("", None, True)
    assert events('{"part":null}') == ("", None, False)


def test_read_only_and_editing_configs_keep_other_tools_denied() -> None:
    worker = MiMoOpenCode(SecretStr("test-only"))
    for read_only in (True, False):
        config = json.loads(worker.config(read_only=read_only, steps=7))
        assert config["agent"]["build"]["steps"] == 7
        assert config["permission"]["*"] == "deny"
        assert config["permission"]["external_directory"] == "deny"
        assert config["permission"]["read"]["*.env"] == "deny"
        assert config["permission"]["edit"] == ("deny" if read_only else "allow")
        assert config["mcp"] == {}
        assert config["plugin"] == []
        assert config["lsp"] is False
        assert config["formatter"] is False
        assert "test-only" not in json.dumps(config)


def test_run_isolates_config_state_and_prompt_then_cleans_it(tmp_path: Path) -> None:
    captured: list[Path] = []

    def runner(
        argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        task = Path(argv[argv.index("--file") + 1])
        captured.append(task.parent)
        assert task.read_text() == "untrusted $(command) task"
        assert "untrusted" not in " ".join(argv)
        assert cwd == tmp_path
        assert stdin == ""
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert env["WORKER_API_KEY"] == "test-only"
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
        assert Path(env["OPENCODE_TEST_HOME"]).parent == task.parent
        for name in ("CONFIG", "DATA", "CACHE", "STATE"):
            assert Path(env[f"XDG_{name}_HOME"]).parent == task.parent
        assert (
            json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["build"]["steps"] == 9
        )
        return 0, json.dumps({"type": "text", "part": {"text": "done"}}), ""

    result = MiMoOpenCode(SecretStr("test-only")).run(
        "untrusted $(command) task",
        tmp_path,
        max_turns=9,
        runner=runner,
        environ={"PATH": "x", "GH_TOKEN": "held-secret", "GITHUB_TOKEN": "held-secret"},
    )
    assert result.text == "done"
    assert result.harness == "opencode"
    assert result.model == "mimo-v2.6-flash"
    assert result.usage is None
    assert not captured[0].exists()


def test_cli_requires_explicit_alternative_harness_selection() -> None:
    settings = Settings(
        mimo_api_key=SecretStr("test-only"),
        mimo_harness="opencode",
        opencode_executable="D:/trusted/opencode.exe",
    )
    adapter = cli.providers(settings)["mimo"]
    assert isinstance(adapter, MiMoOpenCode)
    assert adapter.executable == "D:/trusted/opencode.exe"
