from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_dispatcher.intake import Issue
from agent_dispatcher.queue import RunActiveError, RunQueue, Task

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def issue(number: int) -> Issue:
    return Issue(
        "o/r", number, f"task {number}", "", ("agent:ready",), "owner", is_open=True
    )


@pytest.fixture
def queue(tmp_path: Path):
    opened = RunQueue(tmp_path / "queue.sqlite3")
    yield opened
    opened.close()


def test_enqueue_is_idempotent(queue: RunQueue) -> None:
    assert queue.enqueue(issue(1), NOW)
    assert not queue.enqueue(issue(1), NOW)


def test_oldest_unrun_task_is_next(queue: RunQueue) -> None:
    queue.enqueue(issue(2), NOW + timedelta(minutes=1))
    queue.enqueue(issue(1), NOW)
    assert queue.next_task() == Task("o/r", 1, "task 1")


def test_only_one_run_at_a_time(queue: RunQueue) -> None:
    queue.enqueue(issue(1), NOW)
    queue.enqueue(issue(2), NOW)
    first = queue.start_run(Task("o/r", 1, "task 1"), "claude", NOW)
    with pytest.raises(RunActiveError):
        queue.start_run(Task("o/r", 2, "task 2"), "claude", NOW)
    queue.finish_run(first, "review", NOW)
    assert queue.start_run(Task("o/r", 2, "task 2"), "codex", NOW) > first


def test_a_task_with_a_run_is_not_offered_again(queue: RunQueue) -> None:
    queue.enqueue(issue(1), NOW)
    run = queue.start_run(Task("o/r", 1, "task 1"), "claude", NOW)
    queue.finish_run(run, "failed", NOW)
    assert queue.next_task() is None


def test_the_lock_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    first = RunQueue(path)
    first.start_run(Task("o/r", 1, "t"), "claude", NOW)
    first.close()
    reopened = RunQueue(path)
    with pytest.raises(RunActiveError):
        reopened.start_run(Task("o/r", 2, "t"), "claude", NOW)
    reopened.close()


def test_a_finished_task_labelled_ready_again_is_requeued(queue: RunQueue) -> None:
    queue.enqueue(issue(1), NOW)
    run = queue.start_run(
        Task("o/r", 1, "task 1"), "claude", NOW + timedelta(minutes=1)
    )
    assert not queue.enqueue(issue(1), NOW + timedelta(minutes=2))
    queue.finish_run(run, "failed", NOW + timedelta(minutes=3))
    assert queue.next_task() is None
    assert queue.enqueue(issue(1), NOW + timedelta(minutes=4))
    assert queue.next_task() == Task("o/r", 1, "task 1")
    assert not queue.enqueue(issue(1), NOW + timedelta(minutes=5))


def test_a_parked_task_is_not_requeued_but_still_waits(queue: RunQueue) -> None:
    queue.enqueue(issue(1), NOW)
    run = queue.start_run(
        Task("o/r", 1, "task 1"), "claude", NOW + timedelta(minutes=1)
    )
    queue.finish_run(run, "parked", NOW + timedelta(minutes=2))
    assert not queue.enqueue(issue(1), NOW + timedelta(minutes=3))
    assert queue.next_task() == Task("o/r", 1, "task 1")
