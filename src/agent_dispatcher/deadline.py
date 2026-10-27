"""Keep subprocess waits tied to UTC deadlines, including across host suspension."""

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """Return wall time rather than time spent awake in a platform wait."""
    return datetime.now(UTC)


def communicate(
    process: subprocess.Popen[bytes],
    timeout: float,
    stdin: bytes | None = None,
    *,
    now: Callable[[], datetime] = utc_now,
) -> tuple[bytes, bytes]:
    """Recheck an absolute deadline at least every five awake seconds."""
    deadline = now() + timedelta(seconds=timeout)
    pending = stdin
    while True:
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.communicate(pending, timeout=min(5, remaining))
        except subprocess.TimeoutExpired:
            # Retrying communicate preserves captured output; stdin is sent once.
            pending = None
