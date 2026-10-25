"""One poll cycle: read ready issues, queue the owner's, pick what runs next."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_dispatcher.intake import Issue, accepts
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.quota import Quota, allows

# Phase 1 routes coding to Claude and falls back to Codex when Claude is parked.
CODING_ORDER = ("claude", "codex")


@dataclass(frozen=True)
class Plan:
    """What one cycle decided. Nothing has been run or written yet."""

    queued: list[Issue]
    ignored: list[Issue]
    next_task: Task | None
    provider: str | None


def choose_provider(
    quotas: Mapping[str, Quota], max_share: float, now: datetime
) -> str | None:
    """Return the first coding provider whose quota allows a run, or None to park."""
    for name in CODING_ORDER:
        quota = quotas.get(name)
        if quota is not None and allows(quota, max_share, now):
            return name
    return None


@dataclass(frozen=True)
class Cycle:
    """The inputs a cycle needs, so tests can swap each one."""

    owner: str
    repos: Sequence[str]
    read_issues: Callable[[str], list[Issue]]
    queue: RunQueue
    max_share: float

    def plan(self, quotas: Mapping[str, Quota], now: datetime) -> Plan:
        """Poll every repo, queue owner-labelled issues, and pick the next run."""
        queued: list[Issue] = []
        ignored: list[Issue] = []
        for repo in self.repos:
            for issue in self.read_issues(repo):
                if not accepts(issue, self.owner):
                    ignored.append(issue)
                elif self.queue.enqueue(issue, now):
                    queued.append(issue)
        task = self.queue.next_task()
        provider = choose_provider(quotas, self.max_share, now) if task else None
        return Plan(queued, ignored, task, provider)
