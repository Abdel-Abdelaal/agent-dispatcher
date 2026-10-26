"""One poll cycle: read ready issues, queue the owner's, pick what runs next."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_dispatcher.intake import Issue, accepts
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.quota import Quota, allows
from agent_dispatcher.roles import Team, form_team


@dataclass(frozen=True)
class Plan:
    """What one cycle decided. Nothing has been run or written yet."""

    queued: list[Issue]
    ignored: list[Issue]
    next_task: Task | None
    team: Team | None


def choose_team(
    quotas: Mapping[str, Quota],
    max_share: float,
    now: datetime,
    *,
    allow_worker_thinking: bool = False,
) -> Team | None:
    """Return the team for the next run; providers missing from `quotas` are off."""
    return form_team(
        lambda name: name in quotas and allows(quotas[name], max_share, now),
        allow_worker_thinking=allow_worker_thinking,
    )


@dataclass(frozen=True)
class Cycle:
    """The inputs a cycle needs, so tests can swap each one."""

    owner: str
    repos: Sequence[str]
    read_issues: Callable[[str], list[Issue]]
    queue: RunQueue
    max_share: float
    allow_worker_thinking: bool = False

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
        team = (
            choose_team(
                quotas,
                self.max_share,
                now,
                allow_worker_thinking=self.allow_worker_thinking,
            )
            if task
            else None
        )
        return Plan(queued, ignored, task, team)
