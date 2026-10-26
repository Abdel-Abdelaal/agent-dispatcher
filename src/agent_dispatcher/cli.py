"""Command line: `agent-dispatcher once [--dry-run]`."""

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from agent_dispatcher.dispatcher import Cycle, Plan
from agent_dispatcher.execute import Outcome, Provider, Run
from agent_dispatcher.gate import Limits
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.providers import (
    ChatApi,
    ClaudeCli,
    CodexCli,
    Tuning,
    mimo,
    mimo_worker,
    nvidia,
    nvidia_worker,
    subprocess_runner,
)
from agent_dispatcher.publish import GitHubWriter
from agent_dispatcher.queue import RunQueue
from agent_dispatcher.quota import Quota
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import State, state_of
from agent_dispatcher.workspace import Workspace


def configured(settings: Settings) -> list[str]:
    """Return the providers this host can use: the CLIs, plus workers with a key."""
    names = ["claude", "codex"]
    names += ["mimo"] if settings.mimo_api_key else []
    names += ["nvidia"] if settings.nvidia_api_key else []
    return names


def quotas(names: Sequence[str], parked: dict[str, datetime]) -> dict[str, Quota]:
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
        for name in names
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
    elif plan.team is None:
        lines.append(f"next: {plan.next_task.repo}#{plan.next_task.number} parked")
    else:
        task, team = plan.next_task, plan.team
        mode = " (dry run)" if dry_run else ""
        lines.append(
            f"next: {task.repo}#{task.number}: {team.planner} plans, "
            f"{team.worker} implements, {team.reviewer} reviews{mode}"
        )
    return "\n".join(lines)


def providers(settings: Settings) -> dict[str, Provider]:
    """Return an adapter for every configured provider."""
    adapters: dict[str, Provider] = {
        "claude": ClaudeCli(
            oauth_token=settings.claude_oauth_token,
            work=Tuning(settings.claude_work_model, settings.claude_work_effort),
        ),
        "codex": CodexCli(
            work=Tuning(settings.codex_work_model, settings.codex_work_effort)
        ),
    }
    if settings.mimo_api_key:
        adapters["mimo"] = mimo_worker(settings.mimo_api_key)
    if settings.nvidia_api_key:
        adapters["nvidia"] = nvidia_worker(settings.nvidia_api_key)
    return adapters


def triage(
    settings: Settings, client: httpx.Client
) -> Callable[[str, str], str] | None:
    """Return the cheapest chat model for triage: MiMo, else NVIDIA, else none."""
    chat: ChatApi | None = None
    if settings.mimo_api_key:
        chat = mimo(settings.mimo_api_key)
    elif settings.nvidia_api_key:
        chat = nvidia(settings.nvidia_api_key)
    if chat is None:
        return None
    model = chat
    return lambda system, user: model.complete(system, user, client)


def run_live(
    plan: Plan,
    settings: Settings,
    reader: GitHubReader,
    queue: RunQueue,
    client: httpx.Client,
) -> Outcome | None:
    """Run the planned task once, recording the run and parking a spent provider."""
    if plan.next_task is None or plan.team is None:
        return None
    task, team = plan.next_task, plan.team
    issue = reader.issue(task.repo, task.number)
    root = settings.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    run_id = queue.start_run(task, "+".join(team.members()), now)
    outcome = Outcome(State.FAILED, "controller error")
    try:
        outcome = Run(
            issue=issue,
            state=state_of(issue.labels) or State.READY,
            run_id=run_id,
            team=team,
            providers=providers(settings),
            workspace=Workspace(root, subprocess_runner, os.environ),
            writer=GitHubWriter(subprocess_runner, os.environ, root),
            limits=Limits(settings.max_files, settings.max_lines),
            triage=triage(settings, client),
            ladder=settings.thinking_ladder,
        ).execute()
    finally:
        queue.finish_run(run_id, outcome.state.name.lower(), datetime.now(UTC))
    if outcome.parked is not None:
        until = outcome.resets_at or now + timedelta(hours=settings.park_hours)
        queue.park(outcome.parked, until)
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
            known = quotas(configured(settings), queue.parked_until())
            plan = cycle.plan(known, datetime.now(UTC))
            sys.stdout.write(describe(plan, dry_run=arguments.dry_run) + "\n")
            if not arguments.dry_run:
                outcome = run_live(plan, settings, reader, queue, client)
                if outcome is not None:
                    sys.stdout.write(f"result: {outcome.state} {outcome.detail}\n")
    finally:
        queue.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
