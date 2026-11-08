"""On a hard task the worker makes more than one change and the controller keeps one."""

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_dispatcher.execute import (
    CANDIDATE_CALL,
    CANDIDATE_RESERVE,
    DEADLINE_RESERVE,
    WORK_SECONDS,
    Run,
    changed_tests,
)
from agent_dispatcher.levels import Level
from agent_dispatcher.providers import RunResult, Tuning
from agent_dispatcher.sandbox import COMMAND_FILE, STARTED, Sandbox, pytest_command
from agent_dispatcher.states import State
from agent_dispatcher.telemetry import Telemetry
from test_live import APPROVE, REJECT, FakeGh, FakeModel, git, team_run
from test_run_checks import make_origin
from test_time_budget import Timed


class TreeDocker:
    """Answers each sandbox run by its command and the copied tree.

    The checks pass unless `checks_fail` names the tree's calc.py text. A run of
    named test files passes when the tree's calc.py says "good", unless
    `cross_code` fixes the exit code.
    """

    def __init__(self, checks_fail: str = "", cross_code: int | None = None) -> None:
        self.checks_fail = checks_fail
        self.cross_code = cross_code
        self.cross: list[dict[str, str]] = []

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        del timeout
        if argv[1:3] == ["image", "inspect"]:
            return 0, "ok"
        if argv[1:3] != ["run", "--rm"]:
            return 0, ""
        with tarfile.open(fileobj=io.BytesIO(stdin)) as tar:
            files = {
                member.name: tar.extractfile(member).read().decode()  # type: ignore[union-attr]
                for member in tar.getmembers()
            }
        command = files[COMMAND_FILE]
        calc = files.get("calc.py", "")
        if "pytest -q -p no:cacheprovider -- " in command:
            self.cross.append(files)
            if self.cross_code == 255:  # a sandbox fault, not a test result
                return 255, "ssh: connect to host box port 22: refused"
            code = self.cross_code
            if code is None:
                code = 0 if "good" in calc else 1
            return code, f"{STARTED}\ncross {code}"
        if "agent-tidy-diff-start" in command:
            return 0, f"{STARTED}\n"
        failed = bool(self.checks_fail) and self.checks_fail in calc
        return (1 if failed else 0), f"{STARTED}\nchecks"


class StopsLater(FakeModel):
    """A worker whose coding calls after the first fail or spend its quota."""

    def __init__(self, name: str, edits: list[dict[str, str | None]], *, quota: bool):
        super().__init__(name, edits=edits)
        self.quota = quota
        self.coding = 0

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        if not read_only:
            self.coding += 1
            if self.coding > 1:
                self.code = 1
                self.exhausted = self.quota
        return super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return make_origin(
        tmp_path,
        "calc",
        {
            "pyproject.toml": "[project]\nname = 'calc'\n",
            "uv.lock": "version = 1\n",
            "calc.py": "start\n",
        },
    )


GOOD: dict[str, str | None] = {
    "calc.py": "good\n",
    "tests/test_calc.py": "def test_good(): ...\n",
}
BAD: dict[str, str | None] = {
    "calc.py": "bad\n",
    "tests/test_calc.py": "def test_bad(): ...\n",
}


def hard_run(
    tmp_path: Path,
    origin: Path,
    worker: FakeModel,
    docker: TreeDocker,
    *,
    candidates: int = 2,
) -> tuple[Run, FakeModel, FakeGh]:
    reviewer = FakeModel("codex", [APPROVE])
    gh = FakeGh()
    run = team_run(
        tmp_path, origin, (FakeModel("claude", ["1. Fix"]), worker, reviewer), gh
    )
    run.sandbox = Sandbox(execute=docker)
    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.level = Level.HARD
    run.hard_candidates = candidates
    return run, reviewer, gh


def published(origin: Path, run: Run, name: str) -> str:
    return git(origin, "show", f"{run.branch}:{name}")


def chosen_records(tmp_path: Path) -> list[dict[str, object]]:
    path = next((tmp_path / "telemetry").glob("*.jsonl"))
    records = map(json.loads, path.read_text(encoding="utf-8").splitlines())
    return [r for r in records if r.get("outcome") == "candidate_chosen"]


def test_the_candidate_that_passes_the_others_tests_is_kept(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[BAD, GOOD])
    docker = TreeDocker()
    run, reviewer, gh = hard_run(tmp_path, origin, worker, docker)
    assert run.execute().state is State.REVIEW
    assert len([p for p in worker.prompts if not p[1]]) == 2
    assert published(origin, run, "calc.py") == "good\n"
    # Only the kept candidate reaches the reviewer.
    assert "good" in reviewer.prompts[0][0]
    assert "+bad" not in reviewer.prompts[0][0]
    # Each candidate ran the other's tests in a copy of its own tree.
    assert sorted(f["calc.py"] for f in docker.cross) == ["bad\n", "good\n"]
    for files in docker.cross:
        mine = "def test_good" if files["calc.py"] == "good\n" else "def test_bad"
        assert not files["tests/test_calc.py"].startswith(mine)
    (record,) = chosen_records(tmp_path)
    assert record["chosen"] == 2
    assert record["candidates"] == [
        {
            "candidate": 1,
            "lines": 3,
            "fits": True,
            "checks_passed": True,
            "original_tests_failed": 0,
            "others_tests_passed": 0,
            "others_tests_failed": 1,
            "unfinished": False,
        },
        {
            "candidate": 2,
            "lines": 3,
            "fits": True,
            "checks_passed": True,
            "original_tests_failed": 0,
            "others_tests_passed": 1,
            "others_tests_failed": 0,
            "unfinished": False,
        },
    ]
    assert any(n.startswith("The controller kept candidate 2 (") for n in run.notes)
    assert "| Candidate changes | 2, one kept |" in str(gh.calls)


def test_passing_checks_outrank_the_others_tests(tmp_path: Path, origin: Path) -> None:
    # Candidate 2 passes candidate 1's tests but fails the repository's checks.
    worker = FakeModel("mimo", edits=[BAD, GOOD, GOOD])
    docker = TreeDocker(checks_fail="good")
    run, _, _ = hard_run(tmp_path, origin, worker, docker)
    run.execute()
    assert published(origin, run, "calc.py") == "bad\n"
    assert chosen_records(tmp_path)[0]["chosen"] == 1


def test_a_tie_keeps_the_first_candidate(tmp_path: Path, origin: Path) -> None:
    worker = FakeModel("mimo", edits=[GOOD, {**GOOD, "calc.py": "good too\n"}])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    run.execute()
    assert published(origin, run, "calc.py") == "good\n"


def test_unknown_cross_results_fall_to_the_smaller_change(
    tmp_path: Path, origin: Path
) -> None:
    big: dict[str, str | None] = {**GOOD, "calc.py": "good\nand more\n"}
    worker = FakeModel("mimo", edits=[big, GOOD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker(cross_code=99))
    run.execute()
    assert published(origin, run, "calc.py") == "good\n"


def test_a_sandbox_fault_in_the_cross_check_says_nothing(
    tmp_path: Path, origin: Path
) -> None:
    big: dict[str, str | None] = {**BAD, "calc.py": "bad\nand more\n"}
    worker = FakeModel("mimo", edits=[big, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker(cross_code=255))
    assert run.execute().state is State.REVIEW
    assert published(origin, run, "calc.py") == "bad\n"
    record = chosen_records(tmp_path)[0]
    assert record["chosen"] == 2
    assert record["candidates"] == [
        {
            "candidate": 1,
            "lines": 4,
            "fits": True,
            "checks_passed": True,
            "original_tests_failed": 0,
            "others_tests_passed": 0,
            "others_tests_failed": 0,
            "unfinished": False,
        },
        {
            "candidate": 2,
            "lines": 3,
            "fits": True,
            "checks_passed": True,
            "original_tests_failed": 0,
            "others_tests_passed": 0,
            "others_tests_failed": 0,
            "unfinished": False,
        },
    ]


@pytest.mark.parametrize("level", [Level.EASY, Level.MEDIUM])
def test_below_hard_there_is_one_candidate(
    tmp_path: Path, origin: Path, level: Level
) -> None:
    worker = FakeModel("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    run.level = level
    assert run.execute().state is State.REVIEW
    assert len([p for p in worker.prompts if not p[1]]) == 1
    assert run.candidates == 1


def test_one_candidate_when_turned_off(tmp_path: Path, origin: Path) -> None:
    worker = FakeModel("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker(), candidates=1)
    run.execute()
    assert len([p for p in worker.prompts if not p[1]]) == 1


def test_no_extra_candidate_without_the_sandbox(tmp_path: Path, origin: Path) -> None:
    worker = FakeModel("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    run.sandbox = None
    assert run.execute().state is State.REVIEW
    assert len([p for p in worker.prompts if not p[1]]) == 1


def test_no_extra_candidate_when_time_is_short(tmp_path: Path, origin: Path) -> None:
    worker = FakeModel("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + (DEADLINE_RESERVE * 2)
    assert run.execute().state is State.REVIEW
    assert len([p for p in worker.prompts if not p[1]]) == 1
    assert "Too little of the run's time is left for candidate 2." in run.notes


def test_an_extra_candidate_runs_in_less_than_a_full_hard_call(
    tmp_path: Path, origin: Path
) -> None:
    # 60 minutes left: too little for a full 40-minute call plus the review's 25,
    # enough for a shorter call that still leaves the review its time.
    worker = Timed("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + timedelta(minutes=60)
    assert run.execute().state is State.REVIEW
    assert run.candidates == 2
    first, second = worker.limits[0], worker.limits[1]
    assert first == WORK_SECONDS["hard"]
    left = timedelta(minutes=60) - CANDIDATE_RESERVE
    assert left.total_seconds() - 60 < second <= left.total_seconds()


def test_calls_after_the_extra_candidates_get_the_usual_reserve(
    tmp_path: Path, origin: Path
) -> None:
    # The review asks for a repair; the repair call may use the time the extra
    # candidate had to leave free, so it gets a full hard call again.
    worker = Timed("mimo", edits=[GOOD, BAD, {"calc.py": "good, fixed\n"}])
    run, reviewer, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    reviewer.replies = [REJECT, APPROVE]
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + timedelta(minutes=60)
    assert run.execute().state is State.REVIEW
    assert run.candidates == 2
    assert worker.limits[-1] == WORK_SECONDS["hard"]
    assert published(origin, run, "calc.py") == "good, fixed\n"


def test_no_extra_candidate_without_time_for_a_shorter_call(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[GOOD, BAD])
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    short = CANDIDATE_CALL + CANDIDATE_RESERVE - timedelta(minutes=2)
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + short
    assert run.execute().state is State.REVIEW
    assert run.candidates == 1
    assert "Too little of the run's time is left for candidate 2." in run.notes


def test_a_candidate_that_stops_is_left_out(tmp_path: Path, origin: Path) -> None:
    worker = StopsLater("mimo", [GOOD, BAD], quota=False)
    run, _, _ = hard_run(tmp_path, origin, worker, TreeDocker())
    assert run.execute().state is State.REVIEW
    assert published(origin, run, "calc.py") == "good\n"
    assert run.candidates == 1
    assert any(n.startswith("Candidate 2 stopped and is left out:") for n in run.notes)
    assert chosen_records(tmp_path) == []


def test_a_spent_quota_still_parks_the_run(tmp_path: Path, origin: Path) -> None:
    worker = StopsLater("mimo", [GOOD, BAD], quota=True)
    run, _, gh = hard_run(tmp_path, origin, worker, TreeDocker())
    assert run.execute().state is State.PARKED
    assert not any("pr" in call[0][:2] for call in gh.calls)


def test_changed_tests_takes_only_plain_test_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_bytes(b"a\n")
    (tmp_path / "b_test.py").write_bytes(b"b\n")
    (tmp_path / "calc.py").write_bytes(b"c\n")
    (tmp_path / "tests" / "test_big.py").write_bytes(b"x" * 200_001)
    patch = "".join(
        f"+++ b/{name}\n"
        for name in (
            "tests/test_a.py",
            "b_test.py",
            "calc.py",
            "tests/test_big.py",
            "tests/test_gone.py",
            "tests/test odd.py",
        )
    )
    assert changed_tests(tmp_path, patch) == {
        "tests/test_a.py": "a\n",
        "b_test.py": "b\n",
    }


def test_pytest_command_quotes_each_file() -> None:
    command = pytest_command(["tests/test_a.py", "tests/it's.py"])
    assert "pytest -q -p no:cacheprovider -- tests/test_a.py 'tests/it'\"'\"'s.py'" in (
        command
    )
    assert command.endswith("else exit 99; fi")
