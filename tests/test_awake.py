import pytest

from agent_dispatcher import awake

HOLD = awake.CONTINUOUS | awake.SYSTEM_REQUIRED


def test_the_system_is_held_for_the_block_then_released() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int:
        calls.append(flags)
        return 1

    with awake.kept_awake(setter):
        assert calls == [HOLD]
    assert calls == [HOLD, awake.CONTINUOUS]


def test_the_hold_is_released_when_the_block_fails() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int:
        calls.append(flags)
        return 1

    with pytest.raises(RuntimeError), awake.kept_awake(setter):
        raise RuntimeError
    assert calls == [HOLD, awake.CONTINUOUS]


def test_a_refused_hold_is_not_reset() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int:
        calls.append(flags)
        return 0

    with awake.kept_awake(setter):
        pass
    assert calls == [HOLD]


def test_off_windows_nothing_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(awake.sys, "platform", "linux")
    assert awake.windows_setter() is None
    with awake.kept_awake():
        pass
