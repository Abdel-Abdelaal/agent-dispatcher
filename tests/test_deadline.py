import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from agent_dispatcher.deadline import communicate, utc_now


def test_wait_rechecks_wall_clock_after_host_suspension() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    process = Mock(
        args=["owned"],
        communicate=Mock(side_effect=subprocess.TimeoutExpired("owned", 5)),
    )
    clock = Mock(side_effect=[start, start, start + timedelta(hours=9)])
    with pytest.raises(subprocess.TimeoutExpired):
        communicate(process, 3600, b"prompt", now=clock)
    process.communicate.assert_called_once_with(b"prompt", timeout=5)


def test_polling_sends_stdin_once_and_retains_complete_output() -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    process = Mock(
        communicate=Mock(
            side_effect=[
                subprocess.TimeoutExpired("owned", 5),
                (b"all output", b"error"),
            ]
        )
    )
    clock = Mock(side_effect=[start, start, start + timedelta(seconds=5)])
    assert communicate(process, 20, b"prompt", now=clock) == (b"all output", b"error")
    assert [call.args[0] for call in process.communicate.call_args_list] == [
        b"prompt",
        None,
    ]
    assert utc_now().tzinfo is UTC
