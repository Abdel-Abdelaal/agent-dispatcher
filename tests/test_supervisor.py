from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from agent_dispatcher import cli, supervisor
from agent_dispatcher.intake import Issue
from agent_dispatcher.queue import CycleLease, RunQueue, RunRecord, Task
from agent_dispatcher.settings import Settings
from agent_dispatcher.telemetry import Telemetry

NOW = datetime.now(UTC)


def lease(name: str = "a", *, expired: bool = True) -> CycleLease:
    deadline = NOW + timedelta(hours=-1 if expired else 1)
    return CycleLease(name * 32, 1234, 12.5, deadline.isoformat())


def test_old_queue_migrates_without_losing_history_or_repair_allowances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, "
            "provider TEXT, started_at TEXT, finished_at TEXT, outcome TEXT);"
        )
        db.execute(
            "INSERT INTO runs VALUES(1,'o/r',1,'mimo',?,?,?)",
            (NOW.isoformat(), NOW.isoformat(), "review"),
        )
    queue = RunQueue(path)
    assert queue.recent_runs()[0].outcome == "review"
    queue.record_repair("o/r", 1, "sha", "failed", NOW)
    assert queue.repairs("o/r", 1) == 1
    queue.close()


def test_settle_changes_only_matching_unfinished_lease(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "queue.sqlite3")
    current = lease()
    queue.register_cycle(current)
    old = queue.start_run(Task("o/r", 1, "old"), "mimo", NOW, lease_id=current.lease_id)
    queue.finish_run(old, "review", NOW)
    active = queue.start_run(
        Task("o/r", 2, "current"), "mimo", NOW, lease_id=current.lease_id
    )
    queue.park("codex", NOW + timedelta(days=1))
    queue.record_repair("o/r", 2, "sha", "failed", NOW)
    assert queue.cycle_has_active_run(current.lease_id)
    assert queue.settle_cycle("different", "failed", NOW) == []
    records = queue.settle_cycle(current.lease_id, "cycle_timeout", NOW)
    assert [record.id for record in records] == [active]
    assert queue.recent_runs()[1].outcome == "review"
    assert queue.repairs("o/r", 2) == 1
    assert "codex" in queue.parked_until()
    assert not queue.active_cycles()
    assert not queue.cycle_has_active_run(current.lease_id)
    assert queue.settle_cycle(current.lease_id, "again", NOW) == []
    queue.close()


def test_interrupted_repair_reservation_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = RunQueue(path)
    attempt = queue.record_repair("o/r", 9, "sha", "STARTED", NOW)
    queue.close()
    queue = RunQueue(path)
    assert queue.repairs("o/r", 9) == 1
    queue.finish_repair(attempt, "FAILED", NOW)
    assert queue.repairs("o/r", 9) == 1
    queue.close()


def test_settle_rolls_back_if_database_update_fails(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = RunQueue(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER deny_cycle_update BEFORE UPDATE ON cycles "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    queue.register_cycle(lease())
    with pytest.raises(sqlite3.Error):
        queue.settle_cycle(lease().lease_id, "failed", NOW)
    assert queue.active_cycles() == [lease()]
    queue.close()


@pytest.mark.parametrize(
    ("birth", "running", "expected"),
    [(12.5, True, True), (99.0, True, False), (12.5, False, False)],
)
def test_pid_reuse_is_not_mistaken_for_cycle_owner(
    monkeypatch: pytest.MonkeyPatch, birth: float, *, running: bool, expected: bool
) -> None:
    monkeypatch.setattr(
        supervisor.psutil,
        "Process",
        Mock(
            return_value=Mock(
                create_time=Mock(return_value=birth),
                is_running=Mock(return_value=running),
            )
        ),
    )
    assert supervisor.lease_alive(lease()) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [(psutil.NoSuchProcess(1234), False), (psutil.AccessDenied(1234), True)],
)
def test_unknown_owner_is_retained_not_killed(
    monkeypatch: pytest.MonkeyPatch, error: Exception, *, expected: bool
) -> None:
    monkeypatch.setattr(supervisor.psutil, "Process", Mock(side_effect=error))
    assert supervisor.lease_alive(lease()) is expected


@pytest.mark.parametrize(
    ("expired", "alive", "recoverable"),
    [(False, False, False), (True, True, False), (True, False, True)],
)
def test_orphan_recovery_requires_expired_confirmed_dead_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expired: bool,
    alive: bool,
    recoverable: bool,
) -> None:
    queue = RunQueue(tmp_path / "queue.sqlite3")
    current = lease(expired=expired)
    queue.register_cycle(current)
    queue.start_run(Task("o/r", 1, "task"), "mimo", NOW, lease_id=current.lease_id)
    monkeypatch.setattr(supervisor, "lease_alive", Mock(return_value=alive))
    notified = Mock()
    assert (
        supervisor.recover(queue, Telemetry(tmp_path / "telemetry"), notified)
        is recoverable
    )
    assert notified.call_count == int(recoverable)
    if recoverable:
        assert queue.recent_runs()[0].outcome == "cycle_abandoned"
    queue.close()


def test_wait_owned_kills_only_its_own_child_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(
        communicate=Mock(
            side_effect=[subprocess.TimeoutExpired("owned", 1), (b"out", b"err")]
        )
    )
    killed = Mock()
    monkeypatch.setattr(supervisor, "kill_tree", killed)
    assert supervisor.wait_owned(process, 1) == (124, "out", "err")
    killed.assert_called_once_with(process)


def test_wait_owned_handles_surviving_pipe_without_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(communicate=Mock(side_effect=subprocess.TimeoutExpired("owned", 1)))
    monkeypatch.setattr(supervisor, "kill_tree", Mock())
    assert supervisor.wait_owned(process, 1) == (124, "", "")


def test_wait_owned_returns_normal_child_result() -> None:
    process = Mock(returncode=0, communicate=Mock(return_value=(b"out", b"err")))
    assert supervisor.wait_owned(process, 1) == (0, "out", "err")


@pytest.mark.parametrize("code", [0, 1, 124])
def test_supervised_cycle_has_private_lease_bounded_wait_and_redacted_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    settings = Settings(db_path=tmp_path / "queue.sqlite3")
    process = Mock(pid=1234)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        supervisor.psutil,
        "Process",
        Mock(return_value=Mock(create_time=Mock(return_value=12.5))),
    )

    def wait(_process: subprocess.Popen[bytes], timeout: float) -> tuple[int, str, str]:
        assert timeout == 1800
        queue = RunQueue(settings.db_path)
        current = queue.active_cycles()[0]
        assert current.pid == 1234
        assert len(current.lease_id) == 32
        queue.start_run(Task("o/r", 1, "task"), "mimo", NOW, lease_id=current.lease_id)
        queue.close()
        return code, "sensitive-known-value", ""

    monkeypatch.setattr(supervisor, "wait_owned", wait)
    notified = Mock()
    assert supervisor.cycle(
        settings,
        Telemetry(tmp_path / "telemetry", secrets=["sensitive-known-value"]),
        notified,
        {},
    ) == (1 if code == 0 else code)
    assert "sensitive-known-value" not in capsys.readouterr().out
    assert spawn.call_args.args[0][:3] == [sys.executable, "-m", "agent_dispatcher.cli"]
    assert "DISPATCHER_RUN_LEASE_ID" in spawn.call_args.kwargs["env"]
    assert notified.call_count == 1


def test_supervisor_does_not_duplicate_an_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(db_path=tmp_path / "queue.sqlite3")
    queue = RunQueue(settings.db_path)
    queue.start_run(Task("o/r", 1, "task"), "mimo", NOW)
    queue.close()
    spawn = Mock()
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    assert (
        supervisor.cycle(settings, Telemetry(tmp_path / "telemetry"), Mock(), {}) == 1
    )
    spawn.assert_not_called()


def test_cancelled_supervisor_cleans_own_child_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(db_path=tmp_path / "queue.sqlite3")
    process = Mock(pid=1234, poll=Mock(return_value=None))
    monkeypatch.setattr(supervisor.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        supervisor.psutil,
        "Process",
        Mock(return_value=Mock(create_time=Mock(return_value=12.5))),
    )
    monkeypatch.setattr(supervisor, "wait_owned", Mock(side_effect=KeyboardInterrupt))

    def mark_stopped(_process: subprocess.Popen[bytes]) -> None:
        process.poll.return_value = 1

    killed = Mock(side_effect=mark_stopped)
    monkeypatch.setattr(supervisor, "kill_tree", killed)
    with pytest.raises(KeyboardInterrupt):
        supervisor.cycle(settings, Telemetry(tmp_path / "telemetry"), Mock(), {})
    killed.assert_called_once_with(process)
    queue = RunQueue(settings.db_path)
    assert queue.active_cycles() == []
    queue.close()


def test_successful_cycle_without_orphan_is_not_reported_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(db_path=tmp_path / "queue.sqlite3")
    monkeypatch.setattr(
        supervisor.subprocess, "Popen", Mock(return_value=Mock(pid=1234))
    )
    monkeypatch.setattr(
        supervisor.psutil,
        "Process",
        Mock(return_value=Mock(create_time=Mock(return_value=12.5))),
    )
    monkeypatch.setattr(supervisor, "wait_owned", Mock(return_value=(0, "", "")))
    notify = Mock()
    assert (
        supervisor.cycle(settings, Telemetry(tmp_path / "telemetry"), notify, {}) == 0
    )
    notify.assert_not_called()


def test_failed_termination_does_not_release_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(
        communicate=Mock(side_effect=subprocess.TimeoutExpired("owned", 1)),
        poll=Mock(return_value=None),
    )
    monkeypatch.setattr(supervisor, "kill_tree", Mock())
    with pytest.raises(RuntimeError, match="retain its lease"):
        supervisor.wait_owned(process, 1)


@pytest.mark.parametrize(
    "state", ["agent:ready", "agent:running", "agent:parked", "agent:review"]
)
def test_failure_notice_preserves_review_and_uses_bounded_controller_writes(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    issue = Issue("o/r", 1, "task", "", (state,), "owner")
    monkeypatch.setattr(
        cli, "GitHubReader", Mock(return_value=Mock(issue=Mock(return_value=issue)))
    )
    writer = Mock(move=Mock(return_value=cli.State.RUNNING))
    monkeypatch.setattr(cli, "GitHubWriter", Mock(return_value=writer))
    record = RunRecord(1, "o/r", 1, "mimo", NOW.isoformat(), None, None)
    cli.failure_notice(Settings())(record, "cycle_timeout")
    assert writer.move.call_count == (
        2 if state == "agent:ready" else 0 if state == "agent:review" else 1
    )
    writer.comment.assert_called_once()


def test_failure_notice_failure_does_not_undo_local_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "GitHubReader", Mock(side_effect=ValueError))
    record = RunRecord(1, "o/r", 1, "mimo", NOW.isoformat(), None, None)
    cli.failure_notice(Settings())(record, "cycle_timeout")


def test_supervise_command_uses_supervisor_without_direct_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=124)
    monkeypatch.setattr(cli, "supervised_cycle", run)
    assert cli.main(["supervise"]) == 124
    run.assert_called_once()
