from collections.abc import Callable

from agent_dispatcher.roles import Team, form_team


def available(*names: str) -> Callable[[str], bool]:
    return lambda name: name in names


def test_thinkers_plan_and_review_workers_implement() -> None:
    team = form_team(available("claude", "codex", "mimo", "nvidia"))
    assert team == Team(planner="claude", worker="mimo", reviewer="codex")
    assert team is not None
    assert team.members() == ("claude", "mimo", "codex")


def test_the_second_worker_steps_in() -> None:
    assert form_team(available("claude", "codex", "nvidia")) == Team(
        "claude", "nvidia", "codex"
    )


def test_one_thinker_does_both_thinking_steps() -> None:
    team = form_team(available("codex", "mimo"))
    assert team == Team("codex", "mimo", "codex")
    assert team is not None
    assert team.members() == ("codex", "mimo")


def test_a_thinker_implements_when_no_worker_is_left() -> None:
    assert form_team(available("claude")) == Team("claude", "claude", "claude")


def test_no_thinker_parks_the_task() -> None:
    assert form_team(available("mimo", "nvidia")) is None


def test_owner_enabled_mimo_only_fallback() -> None:
    assert form_team(available("mimo"), allow_worker_thinking=True) == Team(
        "mimo", "mimo", "mimo"
    )
    assert form_team(available(), allow_worker_thinking=True) is None


def test_fallback_still_prefers_available_thinker() -> None:
    assert form_team(available("codex", "mimo"), allow_worker_thinking=True) == Team(
        "codex", "mimo", "codex"
    )
