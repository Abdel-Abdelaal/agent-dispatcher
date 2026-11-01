"""A benchmark of real fleet fixes, graded by tests the team never sees.

Each task is a merged commit that fixed something and changed tests. The team
starts from the commit before it, with only a problem statement, as SWE-bench
does. The fix's own test files are the hidden grader: they must fail before the
fix and pass on it (`validate`), and a run passes when they pass on the team's
change. Grading runs in the sandbox, so nothing a model wrote runs here.
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_dispatcher import cli
from agent_dispatcher.dispatcher import choose_team
from agent_dispatcher.intake import Issue
from agent_dispatcher.publish import GitHubWriter
from agent_dispatcher.sandbox import Sandbox, SandboxResult
from agent_dispatcher.settings import Settings, load_settings
from agent_dispatcher.states import State

REMOTE = "https://github.com/{repo}.git"
CHECKOUT = Path(__file__).resolve().parents[2]
TASKS = CHECKOUT / "bench" / "tasks.json"


class BenchTask(BaseModel):
    """One historical fix and how to grade it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    fix: str = Field(pattern=r"^[0-9a-f]{40}$")
    title: str
    problem: str
    tests: tuple[str, ...] = Field(min_length=1)


def load_tasks(path: Path) -> list[BenchTask]:
    """Return the benchmark's tasks, refusing duplicates."""
    tasks = [
        BenchTask.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]
    if len({task.id for task in tasks}) != len(tasks):
        message = "benchmark task ids must be unique"
        raise ValueError(message)
    return tasks


def git(cwd: Path, *args: str) -> str:
    """Run git without a terminal prompt; raise on failure."""
    done = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", *args],  # noqa: S607 - git is resolved on PATH, as the workspace does
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return done.stdout


def remove(path: Path) -> None:
    """Delete a tree, including git's read-only object files on Windows."""

    def writable(function: Callable[[str], object], name: str, _error: object) -> None:
        Path(name).chmod(stat.S_IWRITE)
        function(name)

    if path.exists():
        shutil.rmtree(path, onexc=writable)


def checkout(task: BenchTask, root: Path, *, fixed: bool) -> Path:
    """Return a fresh checkout of the task's base commit, or of the fix itself."""
    mirror = root / "mirrors" / task.repo.replace("/", "__")
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        git(root, "clone", "-q", "--bare", REMOTE.format(repo=task.repo), str(mirror))
    else:
        git(
            mirror,
            "fetch",
            "-q",
            REMOTE.format(repo=task.repo),
            "+refs/heads/*:refs/heads/*",
        )
    commit = task.fix if fixed else f"{task.fix}^"
    target = root / "checkouts" / f"{task.id}-{'fix' if fixed else 'base'}"
    remove(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    git(root, "clone", "-q", "--no-checkout", str(mirror), str(target))
    git(target, "checkout", "-q", "--detach", commit)
    return target


def hidden_tests(task: BenchTask, fix: Path, into: Path) -> None:
    """Copy the fix's test files over `into`, replacing any version there."""
    for name in task.tests:
        source = fix / name
        if not source.is_file():
            message = f"{task.id}: {name} is not in the fix"
            raise ValueError(message)
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def run_tests(
    sandbox: Sandbox, image: str, tree: Path, task: BenchTask
) -> SandboxResult:
    """Run the hidden tests over `tree` in the sandbox."""
    names = " ".join(task.tests)
    return sandbox.run(image, tree, f"pytest -q -p no:cacheprovider {names}")


def validate(task: BenchTask, sandbox: Sandbox, root: Path) -> dict[str, object]:
    """Check the grader: hidden tests must fail before the fix and pass on it."""
    base = checkout(task, root, fixed=False)
    fix = checkout(task, root, fixed=True)
    image = sandbox.prepare(base)
    hidden_tests(task, fix, base)
    before = run_tests(sandbox, image, base, task)
    after = run_tests(sandbox, image, fix, task)
    return {
        "id": task.id,
        "valid": before.exit_code != 0 and after.exit_code == 0,
        "before": before.exit_code,
        "after": after.exit_code,
    }


def offline_gh(
    argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
) -> tuple[int, str, str]:
    """Stand in for gh: every GitHub write succeeds and goes nowhere."""
    del cwd, stdin, env
    if argv[1:3] == ["pr", "create"]:
        return 0, "https://github.com/bench/pull/0", ""
    return 0, "", ""


def local_remote(task: BenchTask, root: Path) -> Path:
    """Return a local bare repository whose main is the task's base commit."""
    base = checkout(task, root, fixed=False)
    remote = root / "remotes" / f"{task.id}.git"
    remove(remote)
    remote.parent.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "--bare", str(remote))
    git(base, "push", "-q", str(remote), "HEAD:refs/heads/main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def usage(directory: Path, run_id: int) -> dict[str, int]:
    """Sum the run's recorded tokens and model seconds from its telemetry."""
    totals = {"tokens": 0, "model_seconds": 0, "calls": 0}
    for path in directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record: dict[str, Any] = json.loads(line)
            if record.get("run_id") != run_id or not record.get("duration_ms"):
                continue
            spent: dict[str, Any] = record.get("usage") or {}
            totals["tokens"] += sum(
                int(spent.get(key) or 0)
                for key in ("input_tokens", "output_tokens", "reasoning_tokens")
            )
            totals["model_seconds"] += int(record["duration_ms"]) // 1000
            totals["calls"] += 1
    return totals


def attempt(
    task: BenchTask, settings: Settings, root: Path, run_id: int
) -> dict[str, object]:
    """Run the production team on one task offline, then grade what it published."""
    remote = local_remote(task, root)
    issue = Issue(
        task.repo,
        1,
        task.title,
        task.problem,
        (State.READY.value,),
        settings.owner,
        is_open=True,
    )
    known = cli.quotas(cli.configured(settings), {})
    team = choose_team(
        known,
        settings.max_weekly_share,
        datetime.now(UTC),
        allow_worker_thinking=settings.allow_worker_thinking,
        roster=cli.roster(settings),
    )
    if team is None:
        return {"id": task.id, "outcome": "no team", "resolved": False}
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        run = cli.make_run(
            settings, issue, run_id=run_id, team=team, client=client, root=work
        )
        run.remote = str(remote)
        run.writer = GitHubWriter(offline_gh, {}, work)
        run.authorize = lambda _issue, _expected: True
        run.telemetry = replace(cli.telemetry(settings), directory=root / "telemetry")
        started = time.monotonic()
        outcome = run.execute()
        seconds = round(time.monotonic() - started)
    branch = f"agent/{issue.number}-run-{run_id}"
    published = bool(git(remote, "branch", "--list", branch).strip())
    tree = root / "graded" / f"{task.id}-{run_id}"
    remove(tree)
    if published:
        git(root, "clone", "-q", "--branch", branch, str(remote), str(tree))
    else:
        shutil.copytree(work / f"run-{run_id}-agent", tree)
    fix = checkout(task, root, fixed=True)
    hidden_tests(task, fix, tree)
    sandbox = run.sandbox or Sandbox()
    image = run.sandbox_image or sandbox.prepare(checkout(task, root, fixed=False))
    graded = run_tests(sandbox, image, tree, task)
    return {
        "id": task.id,
        "run_id": run_id,
        "team": [team.planner, team.worker, team.reviewer],
        "outcome": outcome.state.value,
        "published": published,
        "hidden_tests_pass": graded.exit_code == 0,
        "resolved": published and graded.exit_code == 0,
        "wall_seconds": seconds,
        **usage(root / "telemetry", run_id),
        "notes": list(run.notes),
    }


def main(argv: list[str] | None = None) -> int:
    """`validate` the graders, or `run` the team on tasks; results print as JSON."""
    parser = argparse.ArgumentParser(prog="agent-dispatcher-bench")
    parser.add_argument("command", choices=["validate", "run"])
    parser.add_argument("--tasks", type=Path, default=TASKS)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--first-run-id", type=int, default=90_000)
    arguments = parser.parse_args(argv)
    settings = load_settings(CHECKOUT / ".env")
    tasks = [
        task
        for task in load_tasks(arguments.tasks)
        if arguments.only is None or task.id in arguments.only
    ]
    sandbox = cli.sandbox(settings)
    if sandbox is None:
        sys.stderr.write(
            "The benchmark grades in the sandbox: set DISPATCHER_SANDBOX.\n"
        )
        return 2
    root = arguments.root.resolve()
    results: list[dict[str, object]] = []
    for index, task in enumerate(tasks):
        if arguments.command == "validate":
            result = validate(task, sandbox, root)
        else:
            result = attempt(task, settings, root, arguments.first_run_id + index)
        results.append(result)
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    if arguments.command == "run":
        resolved = sum(bool(result["resolved"]) for result in results)
        sys.stdout.write(
            json.dumps({"resolved": resolved, "tasks": len(results)}) + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
