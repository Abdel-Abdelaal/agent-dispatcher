"""Acceptance tests written from the issue alone check the change, before review."""

import json
from pathlib import Path

import pytest

from agent_dispatcher.execute import (
    ACCEPTANCE,
    ACCEPTANCE_FAILED,
    ACCEPTANCE_RESULT,
    ACCEPTANCE_STEPS,
    Run,
)
from agent_dispatcher.levels import Level
from agent_dispatcher.providers import RunResult, Tuning
from agent_dispatcher.sandbox import STARTED, Sandbox
from agent_dispatcher.states import State
from agent_dispatcher.telemetry import Telemetry
from test_candidates import TreeDocker
from test_live import APPROVE, FakeGh, FakeModel, team_run
from test_run_checks import make_origin

TEST = "def test_calc():\n    assert open('calc.py').read() == 'good\\n'\n"
NAME = "tests/test_acceptance_calc.py"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A locked repository whose calc.py the issue wants to say "good"."""
    return make_origin(
        tmp_path,
        "calc",
        {
            "pyproject.toml": "[project]\nname = 'calc'\n",
            "uv.lock": "version = 1\n",
            "calc.py": "start\n",
        },
    )


def make_run(
    tmp_path: Path,
    origin: Path,
    worker: FakeModel,
    docker: TreeDocker,
    *,
    reviews: list[str] | None = None,
) -> tuple[Run, FakeModel, FakeGh]:
    reviewer = FakeModel("codex", reviews or [APPROVE])
    gh = FakeGh()
    run = team_run(
        tmp_path, origin, (FakeModel("claude", ["1. Fix"]), worker, reviewer), gh
    )
    run.sandbox = Sandbox(execute=docker)
    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.acceptance_tests = True
    return run, reviewer, gh


def records(tmp_path: Path, outcome: str) -> list[dict[str, object]]:
    path = next((tmp_path / "telemetry").glob("*.jsonl"))
    found = map(json.loads, path.read_text(encoding="utf-8").splitlines())
    return [r for r in found if r.get("outcome") == outcome]


def test_tests_from_the_issue_alone_check_the_change(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{NAME: TEST}, {"calc.py": "good\n"}])
    docker = TreeDocker()
    run, reviewer, gh = make_run(tmp_path, origin, worker, docker)
    assert run.execute().state is State.REVIEW
    written, implemented = worker.prompts
    # Written first, from the issue alone: no plan, in a clone of its own.
    assert written[0].startswith(ACCEPTANCE)
    assert "1. Fix" not in written[0]
    assert not written[1]
    assert worker.turns[0] == ACCEPTANCE_STEPS
    assert (tmp_path / "work" / "run-3-acceptance" / NAME).exists()
    assert not (tmp_path / "work" / "run-3-agent" / NAME).exists()
    assert "1. Fix" in implemented[0]
    # Kept because they failed on the unchanged code, then run on the change.
    before, *after = [files for files in docker.cross if NAME in files]
    assert before["calc.py"] == "start\n"
    assert all(files["calc.py"] == "good\n" for files in after)
    assert ACCEPTANCE_RESULT.strip() in reviewer.prompts[0][0]
    assert (
        "mimo wrote 1 acceptance test file(s) from the issue alone; they fail on "
        "the unchanged code." in run.notes
    )
    (record,) = records(tmp_path, "acceptance_written")
    assert record["tests"] == [NAME]
    body = gh.pr_body()
    assert "| Acceptance tests | 1 file(s) written from the issue alone" in body
    assert "failed on the unchanged code and pass on this change |" in body
    # They check the change; they aren't part of it.
    assert NAME not in body
    assert not (tmp_path / "work" / "run-3-publish-1" / NAME).exists()


def test_failing_acceptance_tests_get_the_worker_one_round(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel(
        "mimo", edits=[{NAME: TEST}, {"calc.py": "bad\n"}, {"calc.py": "good\n"}]
    )
    run, reviewer, gh = make_run(tmp_path, origin, worker, TreeDocker())
    assert run.execute().state is State.REVIEW
    repair = worker.prompts[2][0]
    assert ACCEPTANCE_FAILED.strip() in repair
    assert TEST in repair
    assert (
        "The acceptance tests fail on the change; the worker gets one round to "
        "meet them." in run.notes
    )
    assert "The acceptance tests still fail" not in " ".join(run.notes)
    assert ACCEPTANCE_RESULT.strip() in reviewer.prompts[0][0]
    assert "pass on this change |" in gh.pr_body()
    assert len(records(tmp_path, "acceptance_failed")) == 1


def test_acceptance_tests_still_failing_go_to_the_reviewer_and_the_pr(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel(
        "mimo", edits=[{NAME: TEST}, {"calc.py": "bad\n"}, {"calc.py": "worse\n"}]
    )
    run, reviewer, gh = make_run(tmp_path, origin, worker, TreeDocker())
    # The reviewer may still approve: the tests may be wrong. The PR says so.
    assert run.execute().state is State.REVIEW
    assert ACCEPTANCE_RESULT.strip() in reviewer.prompts[0][0]
    assert (
        "The acceptance tests still fail on the change, so the reviewer was told."
        in run.notes
    )
    assert "still fail on this change; the reviewer saw the failures |" in (
        gh.pr_body()
    )


def test_tests_that_pass_before_the_change_are_dropped(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{NAME: TEST}, {"calc.py": "bad\n"}])
    run, reviewer, gh = make_run(tmp_path, origin, worker, TreeDocker(cross_code=0))
    assert run.execute().state is State.REVIEW
    assert (
        "No acceptance tests were kept: they pass on the unchanged code, so they "
        "test nothing new." in run.notes
    )
    assert len(worker.prompts) == 2
    assert ACCEPTANCE_RESULT.strip() not in reviewer.prompts[0][0]
    assert "Acceptance tests" not in gh.pr_body()
    (record,) = records(tmp_path, "acceptance_dropped")
    assert record["tests"] == [NAME]


def test_no_new_test_file_means_no_acceptance_tests(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{"notes.md": "x\n"}, {"calc.py": "good\n"}])
    run, _, _ = make_run(tmp_path, origin, worker, TreeDocker())
    assert run.execute().state is State.REVIEW
    assert "No acceptance tests were kept: it wrote no new test file." in run.notes
    assert run.acceptance == {}


class BrokenDocker(TreeDocker):
    """Acceptance tests that fail before the change only for a syntax error."""

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        code, output = super().__call__(argv, stdin, timeout)
        if output == f"{STARTED}\ncross 1":
            return 2, f"{STARTED}\nE   SyntaxError: invalid syntax"
        return code, output


def test_tests_broken_by_a_syntax_error_are_dropped(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{NAME: "def (:\n"}, {"calc.py": "good\n"}])
    run, _, _ = make_run(tmp_path, origin, worker, BrokenDocker())
    assert run.execute().state is State.REVIEW
    assert (
        "No acceptance tests were kept: they fail on a syntax error of their own."
        in run.notes
    )


def test_a_sandbox_fault_keeps_no_acceptance_tests(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{NAME: TEST}, {"calc.py": "good\n"}])
    run, _, _ = make_run(tmp_path, origin, worker, TreeDocker(cross_code=255))
    assert run.execute().state is State.REVIEW
    assert "No acceptance tests were kept: they could not be run." in run.notes


class CappedWriter(FakeModel):
    """Writes its tests, then runs out of turns on the first coding call."""

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        result = super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )
        if prompt.startswith(ACCEPTANCE):
            return RunResult(self.name, 1, "", exhausted=False, stop_reason="max_turns")
        return result


def test_a_capped_writer_keeps_its_tests_and_never_raises_the_level(
    tmp_path: Path, origin: Path
) -> None:
    worker = CappedWriter("mimo", edits=[{NAME: TEST}, {"calc.py": "good\n"}])
    run, _, _ = make_run(tmp_path, origin, worker, TreeDocker())
    assert run.execute().state is State.REVIEW
    assert run.level is Level.MEDIUM
    assert run.acceptance == {NAME: TEST}
    assert len(worker.prompts) == 2
    assert (
        "mimo stopped at its 20-step cap while writing acceptance tests; the "
        "controller checks any test files it wrote."
    ) in run.notes
    assert not any("went on without it" in note for note in run.notes)


def test_acceptance_tests_are_off_unless_asked_for(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{"calc.py": "good\n"}])
    run, _, gh = make_run(tmp_path, origin, worker, TreeDocker())
    run.acceptance_tests = False
    assert run.execute().state is State.REVIEW
    assert len(worker.prompts) == 1
    assert "Acceptance tests" not in gh.pr_body()


def test_without_a_sandbox_there_are_no_acceptance_tests(
    tmp_path: Path, origin: Path
) -> None:
    worker = FakeModel("mimo", edits=[{"calc.py": "good\n"}])
    run, _, _ = make_run(tmp_path, origin, worker, TreeDocker())
    run.sandbox = None
    assert run.execute().state is State.REVIEW
    assert len(worker.prompts) == 1


def test_the_candidate_that_passes_the_acceptance_tests_is_kept(
    tmp_path: Path, origin: Path
) -> None:
    # Both candidates pass the checks; only the second meets the issue.
    worker = FakeModel(
        "mimo",
        edits=[
            {NAME: TEST},
            {"calc.py": "bad\n"},
            {"calc.py": "worse\n"},
            {"calc.py": "good\n"},
        ],
    )
    run, _, _ = make_run(tmp_path, origin, worker, TreeDocker())
    run.level = Level.HARD
    run.hard_candidates = 2
    assert run.execute().state is State.REVIEW
    kept = next(note for note in run.notes if note.startswith("The controller kept"))
    assert kept.startswith("The controller kept candidate 2")
    assert "candidate 1: 2 lines, checks passed, acceptance tests failed" in kept
    assert "candidate 2: 2 lines, checks passed, acceptance tests passed" in kept
