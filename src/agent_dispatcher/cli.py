"""Command line: `agent-dispatcher once --dry-run`."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from agent_dispatcher.dispatcher import Cycle, Plan
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.queue import RunQueue
from agent_dispatcher.quota import Quota
from agent_dispatcher.settings import Settings

# Until headless usage can be read (design check 2), quota is learned from runs.
UNKNOWN_QUOTAS = {
    name: Quota(provider=name, used_share=None, resets_at=None)
    for name in ("claude", "codex")
}


def describe(plan: Plan) -> str:
    """Return a short, human-readable account of a cycle."""
    lines = [f"queued: {issue.repo}#{issue.number}" for issue in plan.queued]
    lines += [
        f"ignored (ready label not added by the owner): {issue.repo}#{issue.number}"
        for issue in plan.ignored
    ]
    if plan.next_task is None:
        lines.append("next: nothing queued")
    elif plan.provider is None:
        lines.append(f"next: {plan.next_task.repo}#{plan.next_task.number} parked")
    else:
        task = plan.next_task
        lines.append(f"next: {task.repo}#{task.number} on {plan.provider} (dry run)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one cycle. Only dry runs exist until the owner setup is done."""
    parser = argparse.ArgumentParser(prog="agent-dispatcher")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="run one poll cycle")
    once.add_argument("--dry-run", action="store_true", help="plan without running")
    arguments = parser.parse_args(argv)
    if not arguments.dry_run:
        sys.stderr.write(
            "Live runs need the GitHub App and CLI logins; use --dry-run.\n"
        )
        return 2
    settings = Settings()
    token = settings.github_token.get_secret_value() if settings.github_token else None
    queue = RunQueue(settings.db_path)
    try:
        with httpx.Client() as client:
            cycle = Cycle(
                owner=settings.owner,
                repos=settings.repos,
                read_issues=GitHubReader(client, token).ready_issues,
                queue=queue,
                max_share=settings.max_weekly_share,
            )
            plan = cycle.plan(UNKNOWN_QUOTAS, datetime.now(UTC))
    finally:
        queue.close()
    sys.stdout.write(describe(plan) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
