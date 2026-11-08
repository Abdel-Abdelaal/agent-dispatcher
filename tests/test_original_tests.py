"""The original versions of existing tests a change edited run against it."""

import json
from pathlib import Path
from typing import cast

import pytest

from agent_dispatcher.execute import ORIGINAL_TESTS, Run
from agent_dispatcher.levels import Level
from agent_dispatcher.sandbox import STARTED, Sandbox
from agent_dispatcher.states import State
from agent_dispatcher.telemetry import Telemetry
from test_candidates import TreeDocker
from test_live import APPROVE, FakeGh, FakeModel, git, team_run
from test_run_checks import make_origin

ORIGINAL = "def test_original(): ...\n"


class OriginalDocker(TreeDocker):
    """Also answers runs of the original test file: it fails on "changed" code."""

    def __init__(self, checks_fail: str = "") -> None:
        super().__init__(checks_fail=checks_fail)
        self.original: list[dict[str, str]] = []

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        code, output = super().__call__(argv, stdin, timeout)
        if not self.cross or self.cross[-1].get("test_calc.py") != ORIGINAL:
            return code, output
        files = self.cross.pop()
        self.original.append(files)
        if "changed" in files.get("calc.py", ""):
            return 1, (
                f"{STARTED}\nF\nFAILED test_calc.py::test_original - assert 1\n1 failed"
            )
        return 0, f"{STARTED}\n1 passed"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A locked repository with an existing test file."""
    return make_origin(
        tmp_path,
        "calc",
        {
            "pyproject.toml": "[project]\nname = 'calc'\n",
            "uv.lock": "version = 1\n",
            "calc.py": "start\n",
            "test_calc.py": ORIGINAL,
        },
    )


def make_run(
    tmp_path: Path,
    origin: Path,
    edits: list[dict[str, str | None]],
    docker: OriginalDocker,
    *,
    candidates: int = 1,
) -> tuple[Run, FakeModel]:
    reviewer = FakeModel("codex", [APPROVE])
    run = team_run(
        tmp_path,
        origin,
        (FakeModel("claude", ["1. Fix"]), FakeModel("mimo", edits=edits), reviewer),
        FakeGh(),
    )
    run.sandbox = Sandbox(execute=docker)
    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.level = Level.HARD
    run.hard_candidates = candidates
    return run, reviewer


def records(tmp_path: Path, outcome: str) -> list[dict[str, object]]:
    path = next((tmp_path / "telemetry").glob("*.jsonl"))
    found = map(json.loads, path.read_text(encoding="utf-8").splitlines())
    return [r for r in found if r.get("outcome") == outcome]


def test_the_reviewer_is_told_which_original_tests_fail(
    tmp_path: Path, origin: Path
) -> None:
    docker = OriginalDocker()
    edits: list[dict[str, str | None]] = [
        {"calc.py": "changed\n", "test_calc.py": "def test_rewritten(): ...\n"}
    ]
    run, reviewer = make_run(tmp_path, origin, edits, docker)
    assert run.execute().state is State.REVIEW
    prompt = reviewer.prompts[0][0]
    assert ORIGINAL_TESTS.strip() in prompt
    assert "- test_calc.py::test_original" in prompt
    # The base version ran, read through Git, over the worker's code.
    (files,) = docker.original
    assert files["calc.py"] == "changed\n"
    assert files["test_calc.py"] == ORIGINAL
    assert (
        "The original versions of 1 edited test(s) fail on the change, so the "
        "reviewer was told." in run.notes
    )
    (record,) = records(tmp_path, "original_tests_failed")
    assert record["tests"] == ["test_calc.py::test_original"]


def test_nothing_is_said_when_the_original_tests_pass(
    tmp_path: Path, origin: Path
) -> None:
    docker = OriginalDocker()
    edits: list[dict[str, str | None]] = [
        {"calc.py": "fine\n", "test_calc.py": ORIGINAL + "def test_more(): ...\n"}
    ]
    run, reviewer = make_run(tmp_path, origin, edits, docker)
    assert run.execute().state is State.REVIEW
    assert len(docker.original) == 1
    assert ORIGINAL_TESTS.strip() not in reviewer.prompts[0][0]
    assert records(tmp_path, "original_tests_failed") == []


def test_no_run_when_no_existing_test_was_edited(tmp_path: Path, origin: Path) -> None:
    docker = OriginalDocker()
    edits: list[dict[str, str | None]] = [
        {"calc.py": "changed\n", "test_new.py": "def test_new(): ...\n"}
    ]
    run, reviewer = make_run(tmp_path, origin, edits, docker)
    assert run.execute().state is State.REVIEW
    assert docker.original == []
    assert ORIGINAL_TESTS.strip() not in reviewer.prompts[0][0]


def test_no_run_when_the_checks_failed_before_the_change(
    tmp_path: Path, origin: Path
) -> None:
    # The base's own checks fail, so a failing original test says nothing new.
    docker = OriginalDocker(checks_fail="start")
    edits: list[dict[str, str | None]] = [
        {"calc.py": "changed\n", "test_calc.py": "def test_rewritten(): ...\n"}
    ]
    run, reviewer = make_run(tmp_path, origin, edits, docker)
    assert run.execute().state is State.REVIEW
    assert docker.original == []
    assert ORIGINAL_TESTS.strip() not in reviewer.prompts[0][0]


def test_the_candidate_that_keeps_the_original_tests_is_kept(
    tmp_path: Path, origin: Path
) -> None:
    # Both pass the checks and each other's tests; the first is smaller but
    # breaks a test it rewrote, which outranks size.
    docker = OriginalDocker()
    edits: list[dict[str, str | None]] = [
        {"calc.py": "good, changed\n", "test_calc.py": "def test_rewritten(): ...\n"},
        {
            "calc.py": "good\nkept\nas before\n",
            "test_calc.py": ORIGINAL + "def test_more(): ...\n",
        },
    ]
    run, _ = make_run(tmp_path, origin, edits, docker, candidates=2)
    assert run.execute().state is State.REVIEW
    assert git(origin, "show", f"{run.branch}:calc.py") == "good\nkept\nas before\n"
    (record,) = records(tmp_path, "candidate_chosen")
    assert record["chosen"] == 2
    rows = cast("list[dict[str, object]]", record["candidates"])
    assert [row["original_tests_failed"] for row in rows] == [1, 0]
