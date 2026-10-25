"""The label state machine: each task carries exactly one `agent:*` label."""

from collections.abc import Iterable
from enum import StrEnum


class State(StrEnum):
    """A task's state, stored on GitHub as the issue's `agent:*` label."""

    READY = "agent:ready"
    RUNNING = "agent:running"
    REVIEW = "agent:review"
    PARKED = "agent:parked"
    FAILED = "agent:failed"


# READY is only ever set by the owner; the controller never moves a task back to it.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.READY: frozenset({State.RUNNING, State.PARKED}),
    State.RUNNING: frozenset({State.REVIEW, State.PARKED, State.FAILED}),
    State.PARKED: frozenset({State.RUNNING, State.FAILED}),
    State.REVIEW: frozenset(),
    State.FAILED: frozenset(),
}


class InvalidTransitionError(ValueError):
    """The controller tried a move the state machine doesn't allow."""


class AmbiguousStateError(ValueError):
    """An issue carries more than one `agent:*` label."""


def transition(current: State, target: State) -> State:
    """Return `target` if moving there from `current` is allowed."""
    if target not in TRANSITIONS[current]:
        message = f"{current} cannot move to {target}"
        raise InvalidTransitionError(message)
    return target


def state_of(labels: Iterable[str]) -> State | None:
    """Return the task state named by an issue's labels, or None if it has none."""
    known = {state.value for state in State}
    found = sorted(label for label in set(labels) if label in known)
    if len(found) > 1:
        message = f"more than one agent label: {', '.join(found)}"
        raise AmbiguousStateError(message)
    return State(found[0]) if found else None
