from datetime import UTC, datetime, timedelta

import pytest

from agent_dispatcher.quota import Quota, allows, looks_exhausted

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def test_under_the_weekly_share_runs() -> None:
    assert allows(Quota("claude", 0.3, None), 0.6, NOW)


def test_at_the_weekly_share_parks() -> None:
    assert not allows(Quota("claude", 0.6, None), 0.6, NOW)


def test_unknown_share_runs_and_lets_the_run_report() -> None:
    assert allows(Quota("claude", None, None), 0.6, NOW)


def test_exhausted_parks_until_reset() -> None:
    later = NOW + timedelta(hours=2)
    quota = Quota("codex", 0.9, later, exhausted=True)
    assert not allows(quota, 0.6, NOW)
    assert allows(quota, 0.6, later)


@pytest.mark.parametrize(
    "output",
    [
        "Claude usage limit reached. Your limit will reset at 3pm",
        "Error: 429 Too Many Requests",
        "insufficient_quota",
        "You've hit your rate limit",
        "You've hit your limit · resets 2pm (Europe/London)",
        "Claude AI usage limit reached|1793012400",
        "You're out of extra usage",
    ],
)
def test_exhaustion_messages(output: str) -> None:
    assert looks_exhausted(output)


def test_other_errors_are_not_exhaustion() -> None:
    assert not looks_exhausted("SyntaxError: invalid syntax on line 4290")
