"""The benchmark grades real fixes with their own hidden tests, offline."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_dispatcher import bench
from agent_dispatcher.bench import (
    BenchTask,
    checkout,
    hidden_tests,
    load_tasks,
    local_remote,
    offline_gh,
    usage,
    validate,
)
from agent_dispatcher.sandbox import STARTED, Sandbox

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A local 'GitHub' repository with a bug, then its fix and a new test."""
    repo = tmp_path / "github" / "o" / "r"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "bug")
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix: add numbers")
    monkeypatch.setattr(bench, "REMOTE", str(tmp_path / "github" / "{repo}"))
    return repo, git(repo, "rev-parse", "HEAD")


def task(fix: str, tests: tuple[str, ...] = ("tests/test_calc.py",)) -> BenchTask:
    return BenchTask(
        id="r-1",
        repo="o/r",
        fix=fix,
        title="add is wrong",
        problem="1+2 isn't 3",
        tests=tests,
    )


def test_tasks_load_and_refuse_duplicates_and_bad_fields(tmp_path: Path) -> None:
    good = {
        "id": "a-1",
        "repo": "o/r",
        "fix": "a" * 40,
        "title": "t",
        "problem": "p",
        "tests": ["tests/x.py"],
    }
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([good]), encoding="utf-8")
    assert load_tasks(path)[0].tests == ("tests/x.py",)
    path.write_text(json.dumps([good, good]), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_tasks(path)
    bads: list[dict[str, object]] = [{"fix": "abc"}, {"tests": []}, {"answer": "x"}]
    for bad in [*bads, {"repo": "nope"}]:
        path.write_text(json.dumps([{**good, **bad}]), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_tasks(path)


def test_checkouts_are_the_commit_before_the_fix_or_the_fix(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    _, fix = upstream
    root = tmp_path / "bench"
    base = checkout(task(fix), root, fixed=False)
    fixed = checkout(task(fix), root, fixed=True)
    assert "a - b" in (base / "calc.py").read_text(encoding="utf-8")
    assert not (base / "tests").exists()
    assert "a + b" in (fixed / "calc.py").read_text(encoding="utf-8")
    # A second checkout reuses the mirror and starts clean.
    (base / "stray.txt").write_text("x", encoding="utf-8")
    again = checkout(task(fix), root, fixed=False)
    assert not (again / "stray.txt").exists()


def test_hidden_tests_replace_the_teams_version(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    _, fix = upstream
    fixed = checkout(task(fix), tmp_path / "bench", fixed=True)
    tree = tmp_path / "team"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_calc.py").write_text(
        "def test_add():\n    pass\n", encoding="utf-8"
    )
    hidden_tests(task(fix), fixed, tree)
    assert "add(1, 2) == 3" in (tree / "tests" / "test_calc.py").read_text(
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not in the fix"):
        hidden_tests(task(fix, ("tests/missing.py",)), fixed, tree)


def test_the_local_remote_serves_only_the_base(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    repo, fix = upstream
    remote = local_remote(task(fix), tmp_path / "bench")
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", f"{fix}^")
    # The fix itself never reaches the team's remote.
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "cat-file", "-e", fix)
    assert git(remote, "branch", "--list") == "* main"


class GradingDocker:
    """Fails the hidden tests on a tree whose calc.py still subtracts."""

    def __call__(
        self, argv: list[str], stdin: bytes, timeout: float
    ) -> tuple[int, str]:
        del timeout
        if argv[1:3] == ["run", "--rm"]:
            if b"a - b" in stdin:
                return 1, f"{STARTED}\n1 failed"
            return 0, f"{STARTED}\n1 passed"
        return 0, "ok"


def test_a_valid_task_fails_before_the_fix_and_passes_on_it(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    _, fix = upstream
    result = validate(task(fix), Sandbox(execute=GradingDocker()), tmp_path / "bench")
    assert result == {"id": "r-1", "valid": True, "before": 1, "after": 0}


def test_github_writes_go_nowhere() -> None:
    assert offline_gh(["gh", "pr", "create"], Path(), "", {})[1].startswith("https://")
    assert offline_gh(["gh", "issue", "edit", "1"], Path(), "", {}) == (0, "", "")


def test_usage_sums_only_the_runs_timed_calls(tmp_path: Path) -> None:
    rows = [
        {
            "run_id": 7,
            "duration_ms": 61_000,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        {"run_id": 7, "duration_ms": 2_000, "usage": {"reasoning_tokens": 1}},
        {"run_id": 7, "stage": "gate"},
        {"run_id": 8, "duration_ms": 9_000, "usage": {"input_tokens": 100}},
    ]
    (tmp_path / "a.jsonl").write_text(
        "\n".join(map(json.dumps, rows)), encoding="utf-8"
    )
    assert usage(tmp_path, 7) == {"tokens": 16, "model_seconds": 63, "calls": 2}


def test_the_benchmark_refuses_to_run_without_a_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "tasks.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bench, "CHECKOUT", tmp_path)
    assert bench.main(["run", "--tasks", str(path), "--root", str(tmp_path)]) == 2
    assert "DISPATCHER_SANDBOX" in capsys.readouterr().err
