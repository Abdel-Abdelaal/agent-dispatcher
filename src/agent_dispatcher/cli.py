"""Command line: `agent-dispatcher once [--dry-run]`."""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from agent_dispatcher.dispatcher import Cycle, Plan, choose_team
from agent_dispatcher.execute import Outcome, Provider, Run
from agent_dispatcher.gate import Limits
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.intake import Issue
from agent_dispatcher.native_evidence import report as evidence_report
from agent_dispatcher.opencode_worker import OpenCodeWorker
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
from agent_dispatcher.publish import GitHubWriter, PublishError
from agent_dispatcher.queue import RunQueue, RunRecord, Task
from agent_dispatcher.quota import Quota
from agent_dispatcher.roles import THINKERS, WORKERS, Roster, Team
from agent_dispatcher.settings import BUILT_IN_PROVIDERS, Settings
from agent_dispatcher.states import State, state_of
from agent_dispatcher.supervisor import cycle as supervised_cycle
from agent_dispatcher.telemetry import (
    Telemetry,
    command_scanner,
    controller_revision,
)
from agent_dispatcher.workspace import Workspace


def target_repos(settings: Settings, reader: GitHubReader) -> list[str]:
    """Return the repositories to watch: discovered with "auto", else as listed.

    The controller's own repository is always excluded, so no agent can edit its gate.
    """
    if [repo.lower() for repo in settings.repos] == ["auto"]:
        repos = reader.owned_public_repos(settings.owner)
    else:
        repos = list(settings.repos)
    excluded = {repo.lower() for repo in settings.exclude_repos}
    return [repo for repo in repos if repo.lower() not in excluded]


def configured(settings: Settings) -> list[str]:
    """Return the providers this host can use: the CLIs, plus workers with a key."""
    names = ["claude", "codex"]
    names += ["mimo"] if settings.mimo_api_key else []
    names += ["nvidia"] if settings.nvidia_api_key else []
    names += [extra.name for extra in settings.extra_providers]
    return [name for name in names if name not in settings.disabled_providers]


def roster(settings: Settings) -> Roster:
    """Return who can think and who can work: built-in providers first, then extras."""
    extras = settings.extra_providers
    return Roster(
        thinkers=THINKERS + tuple(e.name for e in extras if e.role == "thinker"),
        workers=WORKERS + tuple(e.name for e in extras if e.role == "worker"),
    )


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
        "ignored (task lacks one open, owner-authorized ready state): "
        f"{issue.repo}#{issue.number}"
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
    if settings.mimo_api_key and "mimo" not in settings.disabled_providers:
        adapters["mimo"] = (
            OpenCodeWorker(
                settings.mimo_api_key,
                settings.opencode_executable,
                reasoning=settings.mimo_reasoning,
                checkpoint_root=settings.work_root.resolve() / "checkpoints",
            )
            if settings.mimo_harness == "opencode"
            else mimo_worker(settings.mimo_api_key)
        )
    if settings.nvidia_api_key:
        adapters["nvidia"] = nvidia_worker(settings.nvidia_api_key)
    for extra in settings.extra_providers:
        adapters[extra.name] = OpenCodeWorker(
            extra.api_key,
            settings.opencode_executable,
            name=extra.name,
            base_url=extra.base_url,
            model=extra.model,
            model_settings={
                "name": extra.model,
                "limit": {"context": extra.context, "output": extra.output},
            },
            checkpoint_root=settings.work_root.resolve() / "checkpoints",
        )
    return {
        name: adapter
        for name, adapter in adapters.items()
        if name in configured(settings)
    }


def triage(
    settings: Settings, client: httpx.Client
) -> Callable[[str, str], str] | None:
    """Return the cheapest chat model for triage: MiMo, else NVIDIA, else none."""
    chat: ChatApi | None = None
    if settings.mimo_api_key and "mimo" not in settings.disabled_providers:
        chat = mimo(settings.mimo_api_key)
        chat = replace(
            chat,
            extra_body={
                **(chat.extra_body or {}),
                "response_format": {"type": "json_object"},
            },
        )
    elif settings.nvidia_api_key and "nvidia" not in settings.disabled_providers:
        chat = nvidia(settings.nvidia_api_key)
    if chat is None:
        return None
    model = chat
    return lambda system, user: model.complete(system, user, client)


def triage_model(settings: Settings) -> str | None:
    """Return the model triage uses, for telemetry."""
    if settings.mimo_api_key and "mimo" not in settings.disabled_providers:
        return mimo(settings.mimo_api_key).model
    if settings.nvidia_api_key and "nvidia" not in settings.disabled_providers:
        return nvidia(settings.nvidia_api_key).model
    return None


def held_secrets(settings: Settings) -> list[str]:
    """Return every credential the controller holds, so telemetry can scrub it."""
    held = [
        settings.github_token,
        settings.claude_oauth_token,
        settings.mimo_api_key,
        settings.nvidia_api_key,
        *(extra.api_key for extra in settings.extra_providers),
    ]
    return [secret.get_secret_value() for secret in held if secret]


def telemetry(settings: Settings) -> Telemetry:
    """Return the local attempt log under the work root, with its privacy rules."""
    scanner = settings.output_scanner
    return Telemetry(
        settings.work_root.resolve() / "telemetry",
        secrets=held_secrets(settings),
        scanner=command_scanner(scanner) if scanner else None,
        revision=controller_revision(Path(__file__).resolve().parent),
    )


def record_routing(
    settings: Settings, queue: RunQueue, team: Team, run_id: int, task: Task
) -> None:
    """Record every provider the team left out, and why, before the run starts."""
    parked = queue.parked_until()
    sink = telemetry(settings)
    every = (*BUILT_IN_PROVIDERS, *(extra.name for extra in settings.extra_providers))
    for name in every:
        if name in team.members():
            continue
        configured_now = name in configured(settings)
        until = parked.get(name)
        sink.record(
            run_id=run_id,
            attempt=0,
            repo=task.repo,
            issue=task.number,
            stage="route",
            provider=name,
            outcome="skipped",
            reason="parked"
            if until
            else ("unused" if configured_now else "not configured"),
            resets_at=until.isoformat() if until else None,
            available_usage=None,
        )


# One CI repair per agent PR: if that doesn't fix it, a human should look.
MAX_CI_REPAIRS = 1


def make_run(  # noqa: PLR0913 - a run is assembled from every part of the host
    settings: Settings,
    issue: Issue,
    *,
    run_id: int,
    team: Team,
    client: httpx.Client,
    root: Path,
) -> Run:
    """Assemble a run with this host's adapters, git and GitHub access."""
    return Run(
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
        skip_easy_docs_plan=settings.skip_plan_for_easy_docs,
        telemetry=telemetry(settings),
        triage_model=triage_model(settings),
        authorize=lambda original, expected: GitHubReader(
            client,
            settings.github_token.get_secret_value() if settings.github_token else None,
        ).authorized_task(original, settings.owner, expected),
    )


def work_root(settings: Settings) -> Path:
    """Return the directory for clones, creating it if needed."""
    root = settings.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def finish(
    queue: RunQueue,
    run_id: int,
    outcome: Outcome,
    settings: Settings,
    started: datetime,
) -> None:
    """Record how a run ended and park a provider that ran out."""
    queue.finish_run(run_id, outcome.state.name.lower(), datetime.now(UTC))
    if outcome.parked is not None:
        until = outcome.resets_at or started + timedelta(hours=settings.park_hours)
        queue.park(outcome.parked, until)


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
    root = work_root(settings)
    now = datetime.now(UTC)
    run_id = queue.start_run(
        task, "+".join(team.members()), now, lease_id=settings.run_lease_id
    )
    record_routing(settings, queue, team, run_id, task)
    outcome = Outcome(State.FAILED, "controller error")
    try:
        outcome = make_run(
            settings, issue, run_id=run_id, team=team, client=client, root=root
        ).execute()
    finally:
        finish(queue, run_id, outcome, settings, now)
    return outcome


def repair_next(
    settings: Settings,
    reader: GitHubReader,
    queue: RunQueue,
    client: httpx.Client,
) -> str | None:
    """Repair the first agent PR whose blocking checks failed, once per PR."""
    now = datetime.now(UTC)
    known = quotas(configured(settings), queue.parked_until())
    team = choose_team(
        known,
        settings.max_weekly_share,
        now,
        allow_worker_thinking=settings.allow_worker_thinking,
        roster=roster(settings),
    )
    if team is None:
        return None
    for repo in target_repos(settings, reader):
        for pr in reader.agent_prs(repo):
            if queue.repairs(repo, pr.number) >= MAX_CI_REPAIRS:
                continue
            required = reader.required_checks(repo, pr.base)
            failed = reader.failed_checks(repo, pr.sha, required)
            if not failed:
                continue
            root = work_root(settings)
            writer = GitHubWriter(subprocess_runner, os.environ, root)
            with_jobs = [check for check in failed if check.job_id][:3]
            read = [
                (check.name, writer.failed_log(repo, check.job_id))
                for check in with_jobs
            ]
            # A repair with no error text would be a blind guess that uses up
            # one of the PR's attempts, so try again on a later cycle instead.
            if not any(log for _, log in read):
                names = ", ".join(check.name for check in failed)
                sys.stderr.write(
                    f"repair: {repo}#{pr.number} waits, no readable log for {names}\n"
                )
                continue
            logs = "\n\n".join(f"### {name}\n{log}" for name, log in read if log)
            issue = reader.issue(repo, pr.issue)
            task = Task(repo, pr.issue, issue.title)
            run_id = queue.start_run(
                task, "+".join(team.members()), now, lease_id=settings.run_lease_id
            )
            repair_attempt = queue.record_repair(
                repo, pr.number, pr.sha, "STARTED", now
            )
            record_routing(settings, queue, team, run_id, task)
            outcome = Outcome(State.FAILED, "controller error")
            try:
                run = make_run(
                    settings,
                    issue,
                    run_id=run_id,
                    team=team,
                    client=client,
                    root=root,
                )
                outcome = run.repair(pr, logs)
            finally:
                finish(queue, run_id, outcome, settings, now)
                queue.finish_repair(repair_attempt, outcome.state.name, now)
            return f"repair: {repo}#{pr.number} {outcome.state} {outcome.detail}"
    return None


def status(queue: RunQueue, now: datetime) -> str:
    """Return the local state: waiting tasks, parked providers and recent runs."""
    waiting = queue.waiting()
    lines = [f"waiting: {len(waiting)}"]
    lines += [f"  {task.repo}#{task.number} {task.title}" for task in waiting]
    parked = {
        name: until for name, until in queue.parked_until().items() if until > now
    }
    lines.append(f"parked: {len(parked)}")
    lines += [
        f"  {name} until {until.isoformat(timespec='minutes')}"
        for name, until in sorted(parked.items())
    ]
    runs = queue.recent_runs()
    lines.append(f"recent runs: {len(runs)}")
    lines += [
        f"  run {run.id} {run.repo}#{run.number} [{run.team}] "
        f"{run.outcome or 'running'} {run.started_at[:16]}"
        for run in runs
    ]
    return "\n".join(lines)


def failure_notice(settings: Settings) -> Callable[[RunRecord, str], None]:
    """Best-effort bounded GitHub cleanup after an owned controller stops."""

    def notify(record: RunRecord, outcome: str) -> None:
        token = (
            settings.github_token.get_secret_value() if settings.github_token else None
        )
        writer = GitHubWriter(
            partial(subprocess_runner, timeout=15), os.environ, Path.cwd()
        )
        try:
            with httpx.Client() as client:
                issue = GitHubReader(client, token).issue(record.repo, record.number)
            state = state_of(issue.labels)
            if state is State.READY:
                state = writer.move(record.repo, record.number, state, State.RUNNING)
            if state is not None and state in {State.RUNNING, State.PARKED}:
                writer.move(record.repo, record.number, state, State.FAILED)
            writer.comment(
                record.repo,
                record.number,
                f"Controller cycle stopped: {outcome}. Run{record.id} was retained; "
                "this is not a completed task. No automatic retry or merge. "
                f"Files already written remain in `run-{record.id}-agent` under "
                "the controller's work directory, if that clone was created. "
                "The interrupted model conversation cannot yet be resumed. "
                "Inspect those files and any existing draft PR before continuing.",
            )
        except (httpx.HTTPError, PublishError, ValueError):
            sys.stderr.write(
                "Cycle stopped locally; GitHub notice failed. Inspect issue state.\n"
            )

    return notify


def show_evidence(settings: Settings, repo: str, run_id: int) -> int:
    """Check the run's repository before returning local diagnostic metrics."""
    queue = RunQueue(settings.db_path)
    try:
        record = queue.find_run(run_id)
        if record is None or record.repo != repo:
            sys.stderr.write("Run not found for this repository.\n")
            return 1
        data = evidence_report(settings.work_root.resolve(), record.id)
        data.update(run_id=record.id, issue=record.number, outcome=record.outcome)
        sys.stdout.write(json.dumps(data) + "\n")
        return 0
    finally:
        queue.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one cycle: plan, then (unless --dry-run) execute the next task."""
    parser = argparse.ArgumentParser(prog="agent-dispatcher")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="run one poll cycle")
    once.add_argument("--dry-run", action="store_true", help="plan without running")
    commands.add_parser("status", help="show waiting tasks, parked models, runs")
    evidence = commands.add_parser("evidence", help="inspect payload-free run metrics")
    evidence.add_argument("number", type=int)
    evidence.add_argument("--repo", required=True)
    commands.add_parser(
        "supervise", help="run one cycle with an owned process deadline"
    )
    arguments = parser.parse_args(argv)
    settings = Settings()
    if arguments.command == "evidence":
        return show_evidence(settings, arguments.repo, arguments.number)
    if arguments.command == "supervise":
        return supervised_cycle(settings, telemetry(settings), failure_notice(settings))
    if arguments.command == "status":
        queue = RunQueue(settings.db_path)
        try:
            sys.stdout.write(status(queue, datetime.now(UTC)) + "\n")
        finally:
            queue.close()
        return 0
    token = settings.github_token.get_secret_value() if settings.github_token else None
    queue = RunQueue(settings.db_path)
    try:
        with httpx.Client() as client:
            reader = GitHubReader(client, token)
            cycle = Cycle(
                owner=settings.owner,
                repos=target_repos(settings, reader),
                read_issues=reader.ready_issues,
                queue=queue,
                max_share=settings.max_weekly_share,
                allow_worker_thinking=settings.allow_worker_thinking,
                roster=roster(settings),
            )
            known = quotas(configured(settings), queue.parked_until())
            plan = cycle.plan(known, datetime.now(UTC))
            sys.stdout.write(describe(plan, dry_run=arguments.dry_run) + "\n")
            if not arguments.dry_run:
                outcome = run_live(plan, settings, reader, queue, client)
                if outcome is not None:
                    sys.stdout.write(f"result: {outcome.state} {outcome.detail}\n")
                else:
                    repaired = repair_next(settings, reader, queue, client)
                    if repaired is not None:
                        sys.stdout.write(f"{repaired}\n")
    finally:
        queue.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
