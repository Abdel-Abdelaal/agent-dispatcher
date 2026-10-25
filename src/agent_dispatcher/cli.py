"""Command line: `agent-dispatcher once [--dry-run]`."""

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx

from agent_dispatcher.dispatcher import Cycle, Plan
from agent_dispatcher.execute import Outcome, Provider, Run
from agent_dispatcher.gate import Limits
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.providers import ClaudeCli, CodexCli, subprocess_runner
from agent_dispatcher.publish import GitHubWriter
from agent_dispatcher.queue import RunQueue
from agent_dispatcher.quota import Quota
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import State, state_of
from agent_dispatcher.workspace import Workspace


def quotas(parked: dict[str, datetime]) -> dict[str, Quota]:
    """Return what is known: usage shares can't be read headless yet (design check 2).

    So a provider is open until a run reports exhaustion, then parked until reset.
    """
    return {
        name: Quota(
            provider=name,
            used_share=None,
            resets_at=parked.get(name),
            exhausted=name in parked,
        )
        for name in ("claude", "codex")
    }


def describe(plan: Plan, *, dry_run: bool) -> str:
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
        mode = " (dry run)" if dry_run else ""
        lines.append(f"next: {task.repo}#{task.number} on {plan.provider}{mode}")
    return "\n".join(lines)


def provider_for(name: str, settings: Settings) -> Provider:
    """Return the coding adapter for a provider name."""
    if name == "claude":
        return ClaudeCli(oauth_token=settings.claude_oauth_token)
    return CodexCli()


def run_live(
    plan: Plan, settings: Settings, reader: GitHubReader, queue: RunQueue
) -> Outcome | None:
    """Run the planned task once, recording the run and any provider parking."""
    if plan.next_task is None or plan.provider is None:
        return None
    task = plan.next_task
    issue = reader.issue(task.repo, task.number)
    state = state_of(issue.labels) or State.READY
    root = settings.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    run_id = queue.start_run(task, plan.provider, now)
    outcome = Outcome(State.FAILED, "controller error")
    try:
        outcome = Run(
            issue=issue,
            state=state,
            run_id=run_id,
            provider=provider_for(plan.provider, settings),
            workspace=Workspace(root, subprocess_runner, os.environ),
            writer=GitHubWriter(subprocess_runner, os.environ, root),
            limits=Limits(settings.max_files, settings.max_lines),
        ).execute()
    finally:
        queue.finish_run(run_id, outcome.state.name.lower(), datetime.now(UTC))
    if outcome.state is State.PARKED:
        queue.park(plan.provider, now + timedelta(hours=settings.park_hours))
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    """Run one cycle: plan, then (unless --dry-run) execute the next task."""
    parser = argparse.ArgumentParser(prog="agent-dispatcher")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="run one poll cycle")
    once.add_argument("--dry-run", action="store_true", help="plan without running")
    arguments = parser.parse_args(argv)
    settings = Settings()
    token = settings.github_token.get_secret_value() if settings.github_token else None
    queue = RunQueue(settings.db_path)
    try:
        with httpx.Client() as client:
            reader = GitHubReader(client, token)
            cycle = Cycle(
                owner=settings.owner,
                repos=settings.repos,
                read_issues=reader.ready_issues,
                queue=queue,
                max_share=settings.max_weekly_share,
            )
            plan = cycle.plan(quotas(queue.parked_until()), datetime.now(UTC))
            sys.stdout.write(describe(plan, dry_run=arguments.dry_run) + "\n")
            if not arguments.dry_run:
                outcome = run_live(plan, settings, reader, queue)
                if outcome is not None:
                    sys.stdout.write(f"result: {outcome.state} {outcome.detail}\n")
    finally:
        queue.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
