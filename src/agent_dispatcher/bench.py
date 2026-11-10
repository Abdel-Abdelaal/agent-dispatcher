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
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_dispatcher import cli
from agent_dispatcher.awake import kept_awake
from agent_dispatcher.dispatcher import choose_team
from agent_dispatcher.execute import TOKEN_KINDS
from agent_dispatcher.intake import Issue
from agent_dispatcher.publish import GitHubWriter
from agent_dispatcher.sandbox import Sandbox, SandboxError, SandboxResult
from agent_dispatcher.settings import Settings, load_settings
from agent_dispatcher.states import State

REMOTE = "https://github.com/{repo}.git"
CHECKOUT = Path(__file__).resolve().parents[2]
TASKS = CHECKOUT / "bench" / "tasks.json"
# One lock per mirror: two tasks of a repository worked on at once must not
# fetch into the same bare clone together.
_MIRRORS: dict[Path, threading.Lock] = {}
_MIRRORS_LOCK = threading.Lock()


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
    with _MIRRORS_LOCK:
        lock = _MIRRORS.setdefault(mirror, threading.Lock())
    with lock:
        if not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            remote = REMOTE.format(repo=task.repo)
            git(root, "clone", "-q", "--bare", remote, str(mirror))
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


GRADE_TRIES = 3
GRADE_WAIT_SECONDS = 30


def grade(
    sandbox: Sandbox,
    image: str,
    tree: Path,
    task: BenchTask,
    *,
    wait: float = GRADE_WAIT_SECONDS,
) -> SandboxResult | None:
    """Run the hidden tests, trying again when the sandbox itself faults.

    A fault (ssh or docker failing before the tests start) says nothing about
    the change, so it is tried again after `wait` seconds. After GRADE_TRIES
    faults the task is left ungraded (None) rather than stopping the benchmark.
    The fault's detail goes to stderr only: it can name hosts and key paths.
    """
    for number in range(1, GRADE_TRIES + 1):
        try:
            return run_tests(sandbox, image, tree, task)
        except SandboxError as error:
            sys.stderr.write(
                f"{task.id}: grading fault {number} of {GRADE_TRIES}: {error} "
                f"({error.detail[-300:]})\n"
            )
            sys.stderr.flush()
            if number < GRADE_TRIES:
                time.sleep(wait)
    return None


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


def tool_calls(log: Path) -> dict[str, int]:
    """Count the run's sandbox tool calls, and those the sandbox couldn't serve."""
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    entries = [json.loads(line) for line in lines if line.strip()]
    return {
        "sandbox_calls": len(entries),
        "sandbox_unavailable": sum(e.get("outcome") == "unavailable" for e in entries),
    }


def usage(directory: Path, run_id: int) -> dict[str, int]:
    """Sum the run's recorded tokens and model seconds from its telemetry."""
    totals = {"tokens": 0, "model_seconds": 0, "calls": 0}
    for path in directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record: dict[str, Any] = json.loads(line)
            if record.get("run_id") != run_id or not record.get("duration_ms"):
                continue
            spent: dict[str, Any] = record.get("usage") or {}
            totals["tokens"] += sum(int(spent.get(key) or 0) for key in TOKEN_KINDS)
            totals["model_seconds"] += int(record["duration_ms"]) // 1000
            totals["calls"] += 1
    return totals


def attempt(  # noqa: PLR0913 - the grader and the comparison switch ride along
    task: BenchTask,
    settings: Settings,
    root: Path,
    run_id: int,
    grader: Sandbox,
    *,
    team_sandbox: bool = True,
) -> dict[str, object]:
    """Run the production team on one task offline, then grade what it published.

    With `team_sandbox` off the team works as it did before the sandbox: no run
    tool, no checks. Grading always runs in `grader`.
    """
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
    # Each run has its own telemetry folder, so runs side by side never append
    # to the same file.
    telemetry = root / "telemetry" / f"run-{run_id}"
    with httpx.Client() as client:
        run = cli.make_run(
            settings, issue, run_id=run_id, team=team, client=client, root=work
        )
        run.remote = str(remote)
        run.writer = GitHubWriter(offline_gh, {}, work)
        run.authorize = lambda _issue, _expected: True
        run.telemetry = replace(cli.telemetry(settings), directory=telemetry)
        if not team_sandbox:
            run.sandbox = None
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
    image = run.sandbox_image or grader.prepare(checkout(task, root, fixed=False))
    graded = grade(grader, image, tree, task)
    passed = None if graded is None else graded.exit_code == 0
    return {
        "id": task.id,
        "run_id": run_id,
        "team": [team.planner, team.worker, team.reviewer],
        "team_sandbox": team_sandbox,
        "outcome": outcome.state.value,
        "published": published,
        "hidden_tests_pass": passed,
        "resolved": published and passed is True,
        "wall_seconds": seconds,
        **usage(telemetry, run_id),
        **tool_calls(work / f"run-{run_id}-sandbox.jsonl"),
        "notes": list(run.notes),
    }


def side_by_side(
    work: Callable[[int, BenchTask], dict[str, object]],
    tasks: list[BenchTask],
    parallel: int,
) -> list[dict[str, object]]:
    """Run `work` on up to `parallel` tasks at once, printing each result as it ends.

    A task that raises stops the run: tasks not started yet never start, and
    its error is raised once the tasks already running have finished.
    """
    stopped = threading.Event()

    def one(index: int, task: BenchTask) -> dict[str, object] | None:
        if stopped.is_set():
            return None
        try:
            return work(index, task)
        except BaseException:
            stopped.set()
            raise

    results: list[dict[str, object]] = []
    failure: Exception | None = None
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures: list[Future[dict[str, object] | None]] = [
            pool.submit(one, index, task) for index, task in enumerate(tasks)
        ]
        try:
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as error:  # noqa: BLE001 - raised below
                    failure = failure or error
                    continue
                if result is not None:
                    results.append(result)
                    sys.stdout.write(json.dumps(result) + "\n")
                    sys.stdout.flush()
        except BaseException:
            stopped.set()
            raise
    if failure is not None:
        raise failure
    return results


def at_least_one(value: str) -> int:
    """Parse --parallel: a whole number of tasks at once, at least one."""
    number = int(value)
    if number < 1:
        message = "must be at least 1"
        raise argparse.ArgumentTypeError(message)
    return number


def main(argv: list[str] | None = None) -> int:
    """`validate` the graders, or `run` the team on tasks; results print as JSON."""
    parser = argparse.ArgumentParser(prog="agent-dispatcher-bench")
    parser.add_argument("command", choices=["validate", "run"])
    parser.add_argument("--tasks", type=Path, default=TASKS)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--first-run-id", type=int, default=90_000)
    parser.add_argument(
        "--team-sandbox",
        choices=["on", "off"],
        default="on",
        help="off measures the team without the run tool and checks",
    )
    parser.add_argument(
        "--parallel",
        type=at_least_one,
        default=1,
        help="how many tasks to work on at once; each prints when it finishes",
    )
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

    def one(index: int, task: BenchTask) -> dict[str, object]:
        if arguments.command == "validate":
            return validate(task, sandbox, root)
        # The run id follows the task's place in the file, whatever order the
        # tasks finish in.
        return attempt(
            task,
            settings,
            root,
            arguments.first_run_id + index,
            sandbox,
            team_sandbox=arguments.team_sandbox == "on",
        )

    # Hours of model calls; a sleep in the middle would fail the run. The hold
    # is on this thread, which waits until every task is done.
    with kept_awake():
        results = side_by_side(one, tasks, arguments.parallel)
    if arguments.command == "run":
        sys.stdout.write(json.dumps(summary(results)) + "\n")
    return 0


def summary(results: list[dict[str, object]]) -> dict[str, object]:
    """Count what was solved, and how far the team's own "done" can be trusted.

    A published change is the team saying "done". `wrong_done` counts those whose
    hidden tests fail; `correct_unpublished` counts correct work the team didn't
    publish. `done_precision` is the share of graded published changes that were
    right. `ungraded` counts tasks never graded (no team, or the sandbox faulted
    on every try): they are neither right nor wrong.
    """
    graded = [r for r in results if r.get("hidden_tests_pass") is not None]
    resolved = sum(bool(result["resolved"]) for result in results)
    published = sum(bool(result.get("published")) for result in results)
    published_graded = sum(bool(r.get("published")) for r in graded)
    return {
        "resolved": resolved,
        "tasks": len(results),
        "published": published,
        "wrong_done": sum(
            bool(r.get("published")) and r.get("hidden_tests_pass") is False
            for r in results
        ),
        "correct_unpublished": sum(
            not r.get("published") and r.get("hidden_tests_pass") is True
            for r in results
        ),
        "ungraded": len(results) - len(graded),
        "done_precision": (
            round(resolved / published_graded, 3) if published_graded else None
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
