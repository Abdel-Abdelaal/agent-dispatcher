"""Bound the controller's own child, and recover only confirmed dead cycle leases."""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from agent_dispatcher.providers import TIMED_OUT, kill_tree
from agent_dispatcher.queue import CycleLease, RunQueue, RunRecord
from agent_dispatcher.settings import Settings
from agent_dispatcher.telemetry import Telemetry, redact


def lease_alive(lease: CycleLease) -> bool:
    """Check PID plus creation time; PID reuse is not evidence of an active owner."""
    try:
        process = psutil.Process(lease.pid)
        return process.create_time() == lease.birth and process.is_running()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def settle(
    queue: RunQueue,
    lease_id: str,
    outcome: str,
    sink: Telemetry,
    notify: Callable[[RunRecord, str], None],
) -> None:
    """Release only the exact child's lock, preserving quota, repairs and run IDs."""
    for record in queue.settle_cycle(lease_id, outcome, datetime.now(UTC)):
        sink.record(
            run_id=record.id,
            repo=record.repo,
            issue=record.number,
            stage="error",
            provider="controller",
            harness="cycle-supervisor",
            outcome=outcome,
            reason="Owned controller cycle stopped or disappeared.",
        )
        sys.stdout.write(
            f"Run{record.id} stopped: {outcome}. Written files retained; "
            f"https://github.com/{record.repo}/issues/{record.number}\n"
        )
        notify(record, outcome)


def recover(
    queue: RunQueue,
    sink: Telemetry,
    notify: Callable[[RunRecord, str], None],
) -> bool:
    """Recover expired dead leases; retain any live or uncertain owner."""
    for lease in queue.active_cycles():
        if datetime.now(UTC) < datetime.fromisoformat(lease.deadline) or lease_alive(
            lease
        ):
            return False
        settle(queue, lease.lease_id, "cycle_abandoned", sink, notify)
    return not any(row.finished_at is None for row in queue.recent_runs())


def wait_owned(
    process: subprocess.Popen[bytes], timeout: float
) -> tuple[int, str, str]:
    """Bound only the child we started, reusing the provider's process-tree cleanup."""
    try:
        output, error = process.communicate(timeout=timeout)
        code = process.returncode
    except subprocess.TimeoutExpired:
        kill_tree(process)
        try:
            output, error = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output, error = b"", b""
        if process.poll() is None:
            message = "Owned child could not be stopped; retain its lease."
            raise RuntimeError(message) from None
        code = TIMED_OUT
    return code, output.decode("utf-8", "replace"), error.decode("utf-8", "replace")


def cycle(
    settings: Settings,
    sink: Telemetry,
    notify: Callable[[RunRecord, str], None],
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Bound a child we start; never attach to or terminate unrelated PIDs."""
    queue = RunQueue(settings.db_path)
    try:
        if not recover(queue, sink, notify):
            sys.stdout.write(
                "A prior cycle is active or uncertain; no duplicate started.\n"
            )
            return 1
        lease_id = uuid.uuid4().hex
        env = {**environ, "DISPATCHER_RUN_LEASE_ID": lease_id}
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_dispatcher.cli", "once"],
            cwd=Path.cwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        lease = None
        try:
            lease = CycleLease(
                lease_id,
                process.pid,
                psutil.Process(process.pid).create_time(),
                (
                    datetime.now(UTC)
                    + timedelta(seconds=settings.cycle_timeout_seconds)
                ).isoformat(),
            )
            queue.register_cycle(lease)
            code, output, error = wait_owned(process, settings.cycle_timeout_seconds)
        except BaseException:
            # A cancelled supervisor must not leave its own controller running.
            if process.poll() is None:
                kill_tree(process)
            if process.poll() is not None:
                settle(queue, lease_id, "controller_cancelled", sink, notify)
            raise
        outcome = "cycle_timeout" if code == TIMED_OUT else "controller_failed"
        if code == 0:
            outcome = "completed"
            if queue.cycle_has_active_run(lease_id):
                outcome, code = "controller_incomplete", 1
        settle(queue, lease_id, outcome, sink, notify)
        sys.stdout.write(redact(output, sink.secrets)[0])
        sys.stderr.write(redact(error, sink.secrets)[0])
        if code == TIMED_OUT:
            sys.stdout.write(
                "Cycle reached its wall-clock deadline. No automatic task retry.\n"
            )
        return code
    finally:
        queue.close()
