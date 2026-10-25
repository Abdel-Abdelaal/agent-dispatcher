import pytest

from agent_dispatcher.states import (
    TRANSITIONS,
    AmbiguousStateError,
    InvalidTransitionError,
    State,
    state_of,
    transition,
)


def test_every_state_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(State)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (State.READY, State.RUNNING),
        (State.RUNNING, State.REVIEW),
        (State.RUNNING, State.PARKED),
        (State.PARKED, State.RUNNING),
        (State.RUNNING, State.FAILED),
    ],
)
def test_allowed_moves(current: State, target: State) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (State.RUNNING, State.READY),
        (State.REVIEW, State.RUNNING),
        (State.FAILED, State.READY),
        (State.READY, State.REVIEW),
    ],
)
def test_refused_moves(current: State, target: State) -> None:
    with pytest.raises(InvalidTransitionError):
        transition(current, target)


def test_no_state_ever_returns_to_ready() -> None:
    assert all(State.READY not in targets for targets in TRANSITIONS.values())


def test_state_of_reads_the_single_agent_label() -> None:
    assert state_of(["bug", "agent:running"]) is State.RUNNING
    assert state_of(["bug"]) is None


def test_two_agent_labels_are_ambiguous() -> None:
    with pytest.raises(AmbiguousStateError):
        state_of(["agent:ready", "agent:failed"])
