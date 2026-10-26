"""How hard a task is, and how much thinking each level buys.

Triage gives a first rating, the planner can change it, and every rejected
review raises it one step. Easy and medium tasks run on Sonnet; only tasks that
prove hard pay for Opus.
"""

import re
from enum import IntEnum


class Level(IntEnum):
    """A task's difficulty; higher levels buy stronger thinking."""

    EASY = 1
    MEDIUM = 2
    HARD = 3

    @property
    def label(self) -> str:
        """The lower-case name used in prompts, settings and notes."""
        return self.name.lower()


# Thinking model and effort per provider and level: (model, effort). Conserving
# the scarce allowances comes first: the frontier models (Opus, GPT-6-Astra) only
# for hard tasks, and never above high effort.
DEFAULT_LADDER: dict[str, dict[str, tuple[str, str]]] = {
    "claude": {
        "easy": ("claude-sonnet-5-5", "low"),
        "medium": ("claude-sonnet-5-5", "medium"),
        "hard": ("claude-opus-5-5", "high"),
    },
    "codex": {
        "easy": ("gpt-6.1-sol", "low"),
        "medium": ("gpt-6.1-sol", "medium"),
        "hard": ("gpt-6-astra", "high"),
    },
}
# Agent turns allowed per level, as (thinking, working). Easy tasks can't wander;
# hard ones keep room to explore. Only Claude Code's harness enforces a turn cap.
TURNS: dict[str, tuple[int, int]] = {
    "easy": (10, 15),
    "medium": (15, 25),
    "hard": (25, 40),
}
_RATING = re.compile(r"^\s*DIFFICULTY:\s*(easy|medium|hard)\b", re.IGNORECASE)


def parse_level(value: object) -> Level | None:
    """Return the level named by a model's answer, or None if it names none."""
    if not isinstance(value, str):
        return None
    try:
        return Level[value.strip().upper()]
    except KeyError:
        return None


def split_rating(plan: str) -> tuple[Level | None, str]:
    """Return the planner's rating from its first line, and the plan without it."""
    first, _, rest = plan.strip().partition("\n")
    match = _RATING.match(first)
    if match is None:
        return None, plan.strip()
    return parse_level(match.group(1)), rest.strip()


def harder(level: Level) -> Level:
    """Return the next level up, staying at the top."""
    return Level(min(level + 1, Level.HARD))
