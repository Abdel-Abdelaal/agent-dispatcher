"""A run checks the worker's change in the sandbox before anyone reviews it."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_dispatcher.opencode_worker import OpenCodeWorker, SandboxCall
from agent_dispatcher.sandbox import STARTED, Sandbox
from agent_dispatcher.states import State
from agent_dispatcher.telemetry import Telemetry
from test_live import APPROVE, FakeGh, FakeModel, git, team_run


class ScriptedDocker:
    """Answers check runs in order; every other docker call succeeds.

    A check scripted as None is a sandbox fault: no start marker, as when ssh
    or docker fails.
    """

    def __init__(
        self, checks: list[tuple[int, str] | None], *, prepare_fails: bool = False
    ) -> None:
        self.checks = checks
        self.prepare_fails = prepare_fails
        self.runs = 0

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        del stdin, timeout
        if argv[1:3] == ["image", "inspect"]:
            return (1, "") if self.prepare_fails else (0, "ok")
        if argv[1:3] == ["build", "-t"]:
            return 1, "no space left on device"
        if argv[1:3] == ["run", "--rm"]:
            self.runs += 1
            check = self.checks.pop(0)
            if check is None:
                return 255, "ssh: connect to host box port 22: refused"
            return check[0], f"{STARTED}\n{check[1]}"
        return 0, ""


def make_origin(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    for path, text in files.items():
        (seed / path).write_text(text, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    bare = tmp_path / f"{name}.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture
def uv_origin(tmp_path: Path) -> Path:
    """A repository with a locked uv environment, so checks apply."""
    return make_origin(
        tmp_path,
        "uv",
        {
            "pyproject.toml": "[project]\nname = 'a'\n",
            "uv.lock": "version = 1\n",
            "README.md": "hello\n",
        },
    )


@pytest.fixture
def plain_origin(tmp_path: Path) -> Path:
    """A repository without a lockfile."""
    return make_origin(tmp_path, "plain", {"README.md": "hello\n"})


def team(
    edits: list[dict[str, str | None]], reviews: list[str]
) -> tuple[FakeModel, FakeModel, FakeModel]:
    return (
        FakeModel("claude", ["1. Add docs/api.md"]),
        FakeModel("mimo", edits=edits),
        FakeModel("codex", reviews),
    )


def gate_outcomes(tmp_path: Path) -> list[str]:
    path = next((tmp_path / "telemetry").glob("*.jsonl"), None)
    if path is None:
        return []
    return [
        record["outcome"]
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if record["stage"] == "gate"
    ]


def checked_run(
    tmp_path: Path,
    origin: Path,
    models: tuple[FakeModel, FakeModel, FakeModel],
    docker: ScriptedDocker,
):

    run = team_run(tmp_path, origin, models, FakeGh())
    run.sandbox = Sandbox(execute=docker)
    run.telemetry = Telemetry(tmp_path / "telemetry")
    return run


def test_passing_checks_go_to_the_reviewer(tmp_path: Path, uv_origin: Path) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    docker = ScriptedDocker([(0, "baseline ok"), (0, "12 passed")])
    run = checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker)
    assert run.execute().state is State.REVIEW
    assert docker.runs == 2
    assert len(worker.prompts) == 1
    assert (
        "<<<CHECKS>>>\nExit code 0.\n12 passed\n<<<END_CHECKS>>>"
        in reviewer.prompts[0][0]
    )
    assert gate_outcomes(tmp_path)[:1] == ["checks_passed"]


def test_failing_checks_get_one_fix_before_review(
    tmp_path: Path, uv_origin: Path
) -> None:
    planner, worker, reviewer = team(
        [{"docs/api.md": "# API\n"}, {"docs/api.md": "# API fixed\n"}], [APPROVE]
    )
    docker = ScriptedDocker([(0, "baseline ok"), (1, "E501 line too long"), (0, "ok")])
    run = checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker)
    assert run.execute().state is State.REVIEW
    assert len(worker.prompts) == 2
    fix = worker.prompts[1][0]
    assert "The repository's own checks (lint, types, tests) failed" in fix
    assert "<<<CHECKS>>>\nExit code 1.\nE501 line too long\n<<<END_CHECKS>>>" in fix
    assert "<<<CHECKS>>>\nExit code 0.\nok\n<<<END_CHECKS>>>" in reviewer.prompts[0][0]
    assert gate_outcomes(tmp_path)[:2] == ["checks_failed", "checks_passed"]
    assert "The repository's checks failed; the worker fixes them." in run.notes


def test_checks_still_failing_after_the_fix_go_to_the_reviewer(
    tmp_path: Path, uv_origin: Path
) -> None:
    planner, worker, reviewer = team(
        [{"docs/api.md": "a\n"}, {"docs/api.md": "b\n"}], [APPROVE]
    )
    docker = ScriptedDocker([(0, "baseline ok"), (1, "fail one"), (1, "fail two")])
    run = checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker)
    run.execute()
    assert len(worker.prompts) == 2
    assert "Exit code 1.\nfail two" in reviewer.prompts[0][0]


def test_checks_failing_before_any_edit_are_not_the_workers_to_fix(
    tmp_path: Path, uv_origin: Path
) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    docker = ScriptedDocker([(1, "broken on main"), (1, "broken on main")])
    run = checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker)
    assert run.execute().state is State.REVIEW
    assert len(worker.prompts) == 1
    assert "They also failed before the change" in reviewer.prompts[0][0]
    assert "The repository's checks already fail before any change." in run.notes


def test_without_a_lockfile_there_are_no_checks(
    tmp_path: Path, plain_origin: Path
) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    docker = ScriptedDocker([])
    run = checked_run(tmp_path, plain_origin, (planner, worker, reviewer), docker)
    assert run.execute().state is State.REVIEW
    assert docker.runs == 0
    assert "<<<CHECKS>>>" not in reviewer.prompts[0][0]


def test_an_unavailable_sandbox_leaves_the_run_as_before(
    tmp_path: Path, uv_origin: Path
) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    docker = ScriptedDocker([], prepare_fails=True)
    run = checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker)
    assert run.execute().state is State.REVIEW
    assert docker.runs == 0
    assert run.sandbox_image is None
    assert any(note.startswith("Sandbox unavailable") for note in run.notes)
    assert gate_outcomes(tmp_path)[0] == "sandbox_unavailable"


def test_an_opencode_call_gets_the_run_tool_and_is_told_to_use_it(
    tmp_path: Path,
) -> None:
    call = SandboxCall("agent-sandbox:abc", "me@box", Path("C:/k/id"), tmp_path / "l")
    worker = OpenCodeWorker(SecretStr("test-only"), sandbox=call)
    config = json.loads(worker.config(read_only=False, steps=5, worktree=tmp_path))
    server = config["mcp"]["sandbox"]
    assert server["type"] == "local"
    assert server["command"][1:4] == ["-P", "-m", "agent_dispatcher.sandbox_mcp"]
    args = server["command"][4:]
    assert args[args.index("--image") + 1] == "agent-sandbox:abc"
    assert args[args.index("--worktree") + 1] == str(tmp_path)
    assert args[args.index("--host") + 1] == "me@box"
    assert config["permission"]["sandbox_run"] == "allow"
    assert config["permission"]["*"] == "deny"
    assert "test-only" not in json.dumps(config)
    plain = json.loads(OpenCodeWorker(SecretStr("k")).config(read_only=False, steps=5))
    assert plain["mcp"] == {}
    assert "sandbox_run" not in plain["permission"]

    seen: list[str] = []

    def runner(
        argv: list[str], cwd: Path, stdin: str, env: object
    ) -> tuple[int, str, str]:
        del cwd, stdin, env
        seen.append(argv[-1])
        return 0, "", ""

    worker.run("task", tmp_path, runner=runner, environ={})
    assert "with the sandbox run tool" in seen[0]
    OpenCodeWorker(SecretStr("k")).run("task", tmp_path, runner=runner, environ={})
    assert "do not attempt shell commands" in seen[1]


def test_a_run_binds_its_image_to_opencode_calls(
    tmp_path: Path, plain_origin: Path
) -> None:
    planner, _worker, reviewer = team([], [APPROVE])
    run = team_run(
        tmp_path, plain_origin, (planner, FakeModel("mimo"), reviewer), FakeGh()
    )
    run.providers = {**run.providers, "mimo": OpenCodeWorker(SecretStr("k"))}
    run.sandbox = Sandbox(host="me@box")
    run.sandbox_image = "agent-sandbox:abc"
    clone = tmp_path / "run-3-agent"
    bound = run._bound_provider("mimo", "implement", clone)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(bound, OpenCodeWorker)
    assert bound.sandbox == SandboxCall(
        "agent-sandbox:abc", "me@box", None, tmp_path / "run-3-sandbox.jsonl"
    )
    run.sandbox_image = None
    unbound = run._bound_provider("mimo", "implement", clone)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(unbound, OpenCodeWorker)
    assert unbound.sandbox is None


@pytest.mark.parametrize(
    "checks",
    [
        [None],  # the baseline can't run
        [(0, "baseline ok"), None],  # the checks after implementing can't run
    ],
)
def test_a_sandbox_fault_mid_run_is_never_a_failing_check(
    tmp_path: Path, uv_origin: Path, checks: list[tuple[int, str] | None]
) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    run = checked_run(
        tmp_path, uv_origin, (planner, worker, reviewer), ScriptedDocker(checks)
    )
    assert run.execute().state is State.REVIEW
    # No fix round for a failure that isn't the code's, and no false claims.
    assert len(worker.prompts) == 1
    assert "<<<CHECKS>>>" not in reviewer.prompts[0][0]
    assert "They also failed before the change" not in reviewer.prompts[0][0]
    assert "sandbox_unavailable" in gate_outcomes(tmp_path)
    assert run.sandbox_image is None


def test_the_reviewer_is_warned_about_weakened_checks(
    tmp_path: Path, uv_origin: Path
) -> None:
    planner, worker, reviewer = team([{"docs/api.md": "# API\n"}], [APPROVE])
    docker = ScriptedDocker([(0, "baseline ok"), (0, "ok")])
    checked_run(tmp_path, uv_origin, (planner, worker, reviewer), docker).execute()
    assert "conftest.py or tool settings" in reviewer.prompts[0][0]


def test_the_run_tool_server_ignores_modules_planted_in_the_worktree(
    tmp_path: Path,
) -> None:
    worker = OpenCodeWorker(SecretStr("k"), sandbox=SandboxCall("img"))
    config = json.loads(worker.config(read_only=False, steps=5, worktree=tmp_path))
    server = config["mcp"]["sandbox"]
    assert server["command"][1:4] == ["-P", "-m", "agent_dispatcher.sandbox_mcp"]
    assert server["environment"]["PYTHONSAFEPATH"] == "1"
