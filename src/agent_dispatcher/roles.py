"""Who does what: thinkers plan and review, workers implement.

Claude and Codex are the strongest models but have the smallest allowances, so
they spend them on short, read-only thinking: a plan before the work and a review
after it. MiMo and NVIDIA have the most allowance, so they do the long, token-heavy
implementation. Every task uses one of each where it can, and the reviewer is the
other thinker from the planner, so one vendor checks the other's plan.
"""

from collections.abc import Callable
from dataclasses import dataclass

THINKERS = ("claude", "codex")
WORKERS = ("mimo", "nvidia")


@dataclass(frozen=True)
class Roster:
    """Every provider that can think or work, in order of preference."""

    thinkers: tuple[str, ...] = THINKERS
    workers: tuple[str, ...] = WORKERS


@dataclass(frozen=True)
class Team:
    """The providers for one run."""

    planner: str
    worker: str
    reviewer: str

    def members(self) -> tuple[str, ...]:
        """Return each provider once, in the order they act."""
        return tuple(dict.fromkeys((self.planner, self.worker, self.reviewer)))


def form_team(
    available: Callable[[str], bool],
    *,
    allow_worker_thinking: bool = False,
    roster: Roster = Roster(),  # noqa: B008 - frozen, so one shared default is safe
) -> Team | None:
    """Return the best team from available providers, or None to park the task.

    Without a thinker nobody can plan or review, so the task waits. Without a
    worker, a thinker implements as a last resort rather than leaving work idle.
    """
    thinkers = [name for name in roster.thinkers if available(name)]
    if not thinkers and allow_worker_thinking:
        thinkers = [name for name in roster.workers if available(name)]
    if not thinkers:
        return None
    workers = [name for name in roster.workers if available(name)] or thinkers
    planner = thinkers[0]
    reviewer = next((name for name in thinkers if name != planner), planner)
    return Team(planner=planner, worker=workers[0], reviewer=reviewer)
