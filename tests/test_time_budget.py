"""Each model call gets a time limit for its level, never past the run's deadline."""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agent_dispatcher import cli
from agent_dispatcher.execute import DEADLINE_RESERVE, WORK_SECONDS
from agent_dispatcher.intake import Issue
from agent_dispatcher.providers import (
    CALL_TIMEOUT,
    STEP_TIMEOUT_SECONDS,
    TIMED_OUT,
    RunResult,
    Tuning,
    subprocess_runner,
)
from agent_dispatcher.roles import Team
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import State
from test_live import APPROVE, FakeGh, FakeModel, team_run
from test_live import origin as origin  # noqa: PLC0414 - the shared fixture


class Timed(FakeModel):
    """Records the time limit each of its calls was given."""

    def __init__(self, name: str, *args: object, **kwargs: object) -> None:
        super().__init__(name, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.limits: list[float] = []

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        self.limits.append(CALL_TIMEOUT.get())
        return super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )


def timed_team() -> tuple[Timed, Timed, Timed]:
    return (
        Timed("claude", ["1. Add docs/api.md"]),
        Timed("mimo", edits=[{"docs/api.md": "# API\n"}]),
        Timed("codex", [APPROVE]),
    )


@pytest.mark.parametrize("level", ["easy", "medium", "hard"])
def test_coding_calls_get_longer_on_harder_tasks(
    tmp_path: Path, origin: Path, level: str
) -> None:
    planner, worker, reviewer = timed_team()
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage=f'{{"proceed": true, "difficulty": "{level}"}}',
    )
    assert run.execute().state is State.REVIEW
    assert planner.limits == [STEP_TIMEOUT_SECONDS]
    assert worker.limits == [WORK_SECONDS[level]]
    assert reviewer.limits == [STEP_TIMEOUT_SECONDS]
    assert CALL_TIMEOUT.get() == STEP_TIMEOUT_SECONDS


def test_no_call_runs_past_the_runs_deadline(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = timed_team()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + timedelta(minutes=10)
    assert run.execute().state is State.REVIEW
    for limit in planner.limits + worker.limits + reviewer.limits:
        assert 9 * 60 < limit <= 10 * 60


def test_a_run_out_of_time_stops_before_the_next_call(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = timed_team()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.deadline = datetime.now(UTC) + DEADLINE_RESERVE + timedelta(minutes=1)
    outcome = run.execute()
    assert outcome.state is State.FAILED
    assert outcome.detail.startswith(
        "The run's time ran out before claude started planning."
    )
    assert planner.limits == worker.limits == reviewer.limits == []


def test_the_runner_uses_the_current_calls_limit(tmp_path: Path) -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    limit = CALL_TIMEOUT.set(1)
    try:
        code, _stdout, stderr = subprocess_runner(sleeper, tmp_path, "", os.environ)
    finally:
        CALL_TIMEOUT.reset(limit)
    assert code == TIMED_OUT
    assert stderr == "timed out after 1s\n"


def test_a_live_run_ends_before_its_cycle_is_killed(tmp_path: Path) -> None:
    issue = Issue(
        "owner/repo", 7, "Task", "Body", ("agent:ready",), "owner", is_open=True
    )
    settings = Settings(owner="owner", work_root=tmp_path, cycle_timeout_seconds=3600)
    before = datetime.now(UTC)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as client:
        run = cli.make_run(
            settings,
            issue,
            run_id=1,
            team=Team("claude", "mimo", "codex"),
            client=client,
            root=tmp_path,
        )
    assert run.deadline is not None
    assert run.deadline <= before + timedelta(seconds=3600) - cli.CYCLE_MARGIN + (
        datetime.now(UTC) - before
    )
    assert run.deadline >= before + timedelta(seconds=3600) - cli.CYCLE_MARGIN
