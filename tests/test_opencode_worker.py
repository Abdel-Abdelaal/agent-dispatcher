"""The alternative harness keeps limits, credentials and retained state bounded."""

import json
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from agent_dispatcher import cli, native_evidence, opencode_worker
from agent_dispatcher.opencode_worker import MiMoOpenCode, events
from agent_dispatcher.providers import RunResult
from agent_dispatcher.settings import Settings
from agent_dispatcher.telemetry import sha256
from agent_dispatcher.workspace import GitError


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
        assert config["agent"]["build"]["permission"] == config["permission"]
        assert config["permission"]["external_directory"] == "deny"
        assert config["permission"]["read"]["*.env"] == "deny"
        assert config["permission"]["edit"] == ("deny" if read_only else "allow")
        assert config["mcp"] == {}
        assert config["plugin"] == []
        assert config["lsp"] is False
        assert config["formatter"] is False
        assert config["enabled_providers"] == ["worker"]
        assert config["small_model"] == "worker/mimo-v2.6-flash"
        model = config["provider"]["worker"]["models"]["mimo-v2.6-flash"]
        assert model["reasoning"] is True
        assert model["interleaved"] == {"field": "reasoning_content"}
        assert model["limit"] == {"context": 196608, "output": 32000}
        assert config["compaction"] == {"auto": True, "prune": True}
        assert config["provider"]["worker"]["models"]["mimo-v2.6-flash"]["options"] == {
            "thinking": {"type": "enabled"},
        }
        assert "test-only" not in json.dumps(config)


def test_run_isolates_config_state_and_prompt_then_cleans_it(tmp_path: Path) -> None:
    captured: list[Path] = []

    def runner(
        argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        assert argv[argv.index("--title") + 1] == "Agent task"
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
        mimo_reasoning="disabled",
    )
    adapter = cli.providers(settings)["mimo"]
    assert isinstance(adapter, MiMoOpenCode)
    assert adapter.executable == "D:/trusted/opencode.exe"
    assert adapter.checkpoint_root == settings.work_root.resolve() / "checkpoints"
    assert adapter.reasoning == "disabled"
    config = json.loads(adapter.config(read_only=True, steps=7))
    assert config["provider"]["worker"]["models"]["mimo-v2.6-flash"]["options"] == {
        "thinking": {"type": "disabled"},
    }


def test_timeout_retains_private_prompt_and_native_state_but_cleans_cache(
    tmp_path: Path,
) -> None:
    cache: list[Path] = []

    def runner(
        _argv: list[str], _cwd: Path, _stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        cache.append(Path(env["XDG_CACHE_HOME"]).parent)
        data = Path(env["XDG_DATA_HOME"]) / "opencode"
        data.mkdir(parents=True)
        (data / "opencode.db").write_bytes(b"synthetic native state")
        return 124, "", "timed out"

    worker = MiMoOpenCode(
        SecretStr("must-not-persist"),
        checkpoint_root=tmp_path / "checkpoints",
        binding=native_evidence.CallBinding(
            7,
            "implement",
            sha256("raw issue"),
            "a" * 40,
            sha256("initial patch"),
            sha256("policy"),
        ),
    )
    result = worker.run("private task", tmp_path, runner=runner, environ={})
    assert result.stop_reason == "timed_out"
    retained = list((tmp_path / "checkpoints").iterdir())
    assert len(retained) == 1
    assert (retained[0] / "task.md").read_text() == "private task"
    assert (
        retained[0] / "data/opencode/opencode.db"
    ).read_bytes() == b"synthetic native state"
    metadata = (retained[0] / "metadata.json").read_text()
    assert "must-not-persist" not in metadata
    assert "private task" not in metadata
    assert json.loads(metadata)["binding"]["run_id"] == 7
    assert json.loads(metadata)["binding"]["task_sha256"] == sha256("raw issue")
    assert json.loads(metadata)["binding"]["patch_sha256"] == sha256("initial patch")
    assert (retained[0] / "config.sha256").read_text() == sha256(
        worker.config(read_only=False, steps=15)
    )
    assert "raw issue" not in metadata
    assert not cache[0].exists()


@pytest.mark.parametrize("exit_code", [0, 124, 429])
@pytest.mark.parametrize(
    "failure", ["callback", "callback_os", "unexpected", "receipt", "native"]
)
def test_unavailable_candidate_fails_closed_without_losing_quota_or_usage(
    tmp_path: Path,
    exit_code: int,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def snapshot() -> str:
        message = "private error details"
        error = {
            "callback": GitError,
            "callback_os": OSError,
            "unexpected": RuntimeError,
        }.get(failure)
        if error is not None:
            raise error(message)
        return sha256("candidate")

    def storage_failure(*_args: object, **_kwargs: object) -> None:
        message = "storage unavailable"
        raise OSError(message)

    if failure == "receipt":
        monkeypatch.setattr(native_evidence, "finish_candidate", storage_failure)
    if failure == "native":
        monkeypatch.setattr(native_evidence, "finish", storage_failure)

    def runner(
        _argv: list[str],
        _cwd: Path,
        _stdin: str,
        _env: Mapping[str, str],
    ) -> tuple[int, str, str]:
        output = json.dumps(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 10, "output": 2, "reasoning": 4}},
            }
        )
        return exit_code, output, "quota exhausted" if exit_code == 429 else ""

    worker = MiMoOpenCode(
        SecretStr("test-only"),
        checkpoint_root=tmp_path / "checkpoints",
        candidate_snapshot=snapshot,
    )
    result = worker.run("private task", tmp_path, runner=runner, environ={})
    assert result.exit_code != 0
    expected_error = {
        "callback": "snapshot_git_error",
        "callback_os": "snapshot_os_error",
        "unexpected": "snapshot_callback_error",
        "receipt": "receipt_storage_error",
        "native": "native_storage_error",
    }[failure]
    assert result.retention_errors == (expected_error,)
    assert result.exhausted is (exit_code == 429)
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "reasoning_tokens": 4,
    }
    assert result.stop_reason == (
        "timed_out" if exit_code == 124 else "candidate_unavailable"
    )
    profile = next((tmp_path / "checkpoints").iterdir())
    if failure == "receipt":
        assert not (profile / "candidate.json").exists()
    else:
        receipt = json.loads((profile / "candidate.json").read_text())
        assert receipt["status"] == (
            "captured" if failure == "native" else "unavailable"
        )
        assert receipt["patch_sha256"] == (
            sha256("candidate") if failure == "native" else None
        )
        assert "private error details" not in json.dumps(receipt)
        assert receipt["retention_errors"] == [expected_error]
    if failure == "native":
        assert not (profile / "result.json").exists()
    else:
        assert (
            json.loads((profile / "result.json").read_text())["exit_code"] == exit_code
        )


def test_snapshot_without_retained_state_is_refused() -> None:
    with pytest.raises(ValueError, match="retained checkpoint state"):
        MiMoOpenCode(
            SecretStr("test-only"), candidate_snapshot=lambda: sha256("candidate")
        )


def test_retention_failure_cannot_request_another_turn_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        opencode_worker,
        "provider_result",
        Mock(
            return_value=RunResult(
                "mimo", 0, "", exhausted=False, stop_reason="max_turns"
            )
        ),
    )

    def snapshot() -> str:
        message = "snapshot unavailable"
        raise GitError(message)

    def runner(
        _argv: list[str],
        _cwd: Path,
        _stdin: str,
        _env: Mapping[str, str],
    ) -> tuple[int, str, str]:
        return 0, "", ""

    worker = MiMoOpenCode(
        SecretStr("test-only"),
        checkpoint_root=tmp_path / "checkpoints",
        candidate_snapshot=snapshot,
    )
    result = worker.run("task", tmp_path, runner=runner, environ={})
    assert result.stop_reason == "candidate_unavailable"
    assert result.exit_code == 1
