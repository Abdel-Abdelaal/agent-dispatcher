"""Live runs against real local git repositories, with fake providers and gh."""

import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_dispatcher import cli
from agent_dispatcher.dispatcher import Plan
from agent_dispatcher.execute import Outcome, Run, pr_title
from agent_dispatcher.gate import Limits
from agent_dispatcher.intake import Issue
from agent_dispatcher.providers import RunResult, subprocess_runner
from agent_dispatcher.publish import GitHubWriter, PublishError
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import InvalidTransitionError, State
from agent_dispatcher.workspace import GitError, Workspace

ISSUE = Issue(
    "o/r", 7, "Document the API.", "Add docs", ("agent:ready", "documentation"), "owner"
)
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


class FakeProvider:
    def __init__(self, edit: dict[str, str | None], code: int = 0) -> None:
        self.edit = edit
        self.code = code
        self.exhausted = False
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "claude"

    def run(self, prompt: str, worktree: Path) -> RunResult:
        self.prompts.append(prompt)
        for path, text in self.edit.items():
            target = worktree / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        # A hostile agent plants a hook; the controller must never run it.
        hook = worktree / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho pwned > ../pwned\n", encoding="utf-8")
        return RunResult("claude", self.code, "{}", self.exhausted)


class FakeGh:
    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.fail_on = fail_on

    def __call__(
        self, argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        del cwd, env
        self.calls.append((argv, stdin))
        if self.fail_on and self.fail_on in argv:
            return 1, "", "boom"
        if argv[1:3] == ["pr", "create"]:
            return 0, "https://github.com/o/r/pull/9\n", ""
        return 0, "", ""

    def labels(self) -> list[tuple[str, str]]:
        return [
            (
                argv[argv.index("--remove-label") + 1],
                argv[argv.index("--add-label") + 1],
            )
            for argv, _ in self.calls
            if "--add-label" in argv
        ]

    def comments(self) -> list[str]:
        return [
            stdin for argv, stdin in self.calls if argv[1:3] == ["issue", "comment"]
        ]


def make_run(tmp_path: Path, origin: Path, provider: FakeProvider, gh: FakeGh) -> Run:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return Run(
        issue=ISSUE,
        state=State.READY,
        run_id=3,
        provider=provider,
        workspace=Workspace(root, subprocess_runner, GIT_ENV),
        writer=GitHubWriter(gh, {}, root),
        limits=Limits(max_files=5, max_lines=100),
        remote=str(origin),
    )


def test_a_good_change_becomes_a_draft_pr(tmp_path: Path, origin: Path) -> None:
    provider = FakeProvider({"docs/api.md": "# API\n"})
    gh = FakeGh()
    outcome = make_run(tmp_path, origin, provider, gh).execute()
    assert outcome == Outcome(State.REVIEW, "https://github.com/o/r/pull/9")
    assert gh.labels() == [
        ("agent:ready", "agent:running"),
        ("agent:running", "agent:review"),
    ]
    assert "<<<UNTRUSTED_ISSUE>>>" in provider.prompts[0]
    message = git(origin, "log", "-1", "--format=%B", "agent/7-run-3")
    assert message.startswith("docs: document the API\n\nCloses #7")
    assert "Agent-Run: 3" in message
    assert "Co-Authored-By: Claude" in message
    pr = next(argv for argv, _ in gh.calls if argv[1:3] == ["pr", "create"])
    assert pr[pr.index("--base") + 1] == "main"
    assert "--draft" in pr
    assert not (tmp_path / "work" / "pwned").exists()


def test_protected_paths_are_refused_and_explained(
    tmp_path: Path, origin: Path
) -> None:
    provider = FakeProvider({".github/workflows/ci.yml": "on: push\n"})
    gh = FakeGh()
    outcome = make_run(tmp_path, origin, provider, gh).execute()
    assert outcome.state is State.FAILED
    assert "protected path: .github/workflows/ci.yml" in outcome.detail
    assert gh.labels()[-1] == ("agent:running", "agent:failed")
    assert "protected path" in gh.comments()[0]
    assert "agent/7-run-3" not in git(origin, "branch", "--list")


def test_deleting_tests_is_refused(tmp_path: Path, origin: Path) -> None:
    provider = FakeProvider({"tests/test_a.py": None})
    outcome = make_run(tmp_path, origin, provider, FakeGh()).execute()
    assert "test lines deleted" in outcome.detail


def test_no_change_fails(tmp_path: Path, origin: Path) -> None:
    outcome = make_run(tmp_path, origin, FakeProvider({}), FakeGh()).execute()
    assert outcome == Outcome(State.FAILED, "The agent made no changes.")


def test_provider_error_fails(tmp_path: Path, origin: Path) -> None:
    provider = FakeProvider({"a.md": "x"}, code=1)
    outcome = make_run(tmp_path, origin, provider, FakeGh()).execute()
    assert outcome == Outcome(State.FAILED, "claude exited with 1.")


def test_exhausted_provider_parks_the_task(tmp_path: Path, origin: Path) -> None:
    provider = FakeProvider({})
    provider.exhausted = True
    gh = FakeGh()
    outcome = make_run(tmp_path, origin, provider, gh).execute()
    assert outcome.state is State.PARKED
    assert gh.labels()[-1] == ("agent:running", "agent:parked")


def test_a_patch_that_does_not_apply_fails(tmp_path: Path, origin: Path) -> None:
    run = make_run(tmp_path, origin, FakeProvider({"a.md": "x"}), FakeGh())
    clone = run.workspace.clone(str(origin), "elsewhere")
    with pytest.raises(GitError):
        run.workspace.apply(clone, "not a patch")


def test_gh_failures_raise(tmp_path: Path) -> None:
    writer = GitHubWriter(FakeGh(fail_on="comment"), {}, tmp_path)
    with pytest.raises(PublishError):
        writer.comment("o/r", 1, "hi")
    with pytest.raises(InvalidTransitionError):
        writer.move("o/r", 1, State.REVIEW, State.RUNNING)


def test_pr_title_is_conventional_and_bounded() -> None:
    long = Issue("o/r", 1, "Fix " + "x" * 200, "", ("bug",), "owner")
    assert pr_title(long).startswith("chore: fix x")
    assert len(pr_title(long)) <= 72


def test_run_live_records_and_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ParkedRun:
        def __init__(self, **_: object) -> None:
            pass

        def execute(self) -> Outcome:
            return Outcome(State.PARKED, "quota")

    class Reader:
        def issue(self, repo: str, number: int) -> Issue:
            return Issue(repo, number, "t", "", ("agent:ready",), "owner")

    monkeypatch.setattr(cli, "Run", ParkedRun)
    queue = RunQueue(tmp_path / "q.sqlite3")
    task = Task("o/r", 1, "t")
    plan = Plan([], [], task, "claude")
    settings = Settings(work_root=tmp_path / "runs")
    outcome = cli.run_live(plan, settings, Reader(), queue)  # pyright: ignore[reportArgumentType]
    assert outcome == Outcome(State.PARKED, "quota")
    assert "claude" in queue.parked_until()
    assert queue.next_task() is None or queue.next_task() == task
    assert cli.run_live(Plan([], [], None, None), settings, Reader(), queue) is None  # pyright: ignore[reportArgumentType]
    queue.close()


def test_quotas_park_until_reset() -> None:
    until = datetime(2026, 10, 26, tzinfo=UTC)
    quotas = cli.quotas({"claude": until})
    assert quotas["claude"].exhausted
    assert quotas["claude"].resets_at == until
    assert not quotas["codex"].exhausted


def test_provider_for_names() -> None:
    settings = Settings()
    assert cli.provider_for("claude", settings).name == "claude"
    assert cli.provider_for("codex", settings).name == "codex"
