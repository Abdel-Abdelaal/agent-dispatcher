"""Keep Windows from sleeping while a benchmark runs.

A sleep in the middle of a model call stops the run: when the machine wakes, the
call's time limit and the run's deadline have already passed. Windows sleeps on
idle unless a running thread says it needs the system; elsewhere this does nothing.
Scheduled cycles get the same protection from `scripts/cycle-awake.ps1`; a
benchmark runs outside it, for hours.
"""

import ctypes
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager

# SetThreadExecutionState flags: keep the state until reset, and the system on.
CONTINUOUS = 0x80000000
SYSTEM_REQUIRED = 0x00000001

SetState = Callable[[int], int]


def windows_setter() -> SetState | None:
    """Return the thread execution state setter, or None off Windows."""
    if sys.platform != "win32":
        return None
    setter = ctypes.windll.kernel32.SetThreadExecutionState
    setter.argtypes = [ctypes.c_uint32]
    setter.restype = ctypes.c_uint32
    return setter


@contextmanager
def kept_awake(set_state: SetState | None = None) -> Generator[None]:
    """Hold the system awake on this thread until the block ends.

    The request lasts while this thread lives, so a killed process releases it.
    A failed request is ignored: the work runs as it would have without it.
    """
    setter = set_state if set_state is not None else windows_setter()
    held = setter is not None and setter(CONTINUOUS | SYSTEM_REQUIRED) != 0
    try:
        yield
    finally:
        if held and setter is not None:
            setter(CONTINUOUS)
