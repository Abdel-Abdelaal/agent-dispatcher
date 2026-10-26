"""Live team runs against real local git repositories, with fake models and gh."""

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agent_dispatcher import cli
from agent_dispatcher.dispatcher import Plan
from agent_dispatcher.execute import Outcome, Provider, Run, json_reply, pr_title
from agent_dispatcher.gate import Limits
from agent_dispatcher.intake import Issue
from agent_dispatcher.levels import DEFAULT_LADDER, Level
from agent_dispatcher.providers import ClaudeCli, RunResult, Tuning, subprocess_runner
from agent_dispatcher.publish import GitHubWriter, PublishError
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.roles import Team
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import InvalidTransitionError, State
from agent_dispatcher.workspace import GitError, Workspace

ISSUE = Issue(
    "o/r", 7, "Document the API.", "Add docs", ("agent:ready", "documentation"), "owner"
)
TEAM = Team(planner="claude", worker="mimo", reviewer="codex")
APPROVE = json.dumps({"approve": True, "summary": "Looks right.", "findings": []})
REJECT = json.dumps({"approve": False, "summary": "Wrong.", "findings": ["typo"]})
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


class FakeModel:
    """Replies in order; edits files when asked to implement."""

    def __init__(
        self,
        name: str,
        replies: list[str] | None = None,
        edits: list[dict[str, str | None]] | None = None,
    ) -> None:
        self._name = name
        self.replies = replies or []
        self.edits = edits or []
        self.prompts: list[tuple[str, bool]] = []
        self.tunings: list[Tuning | None] = []
        self.code = 0
        self.exhausted = False

    @property
    def name(self) -> str:
        return self._name

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
    ) -> RunResult:
        self.prompts.append((prompt, read_only))
        self.tunings.append(tuning)
        if not read_only and self.edits:
            for path, text in self.edits.pop(0).items():
                target = worktree / path
                if text is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
            # A hostile agent plants a hook; the controller must never run it.
            hook = worktree / ".git" / "hooks" / "pre-push"
            hook.write_text("#!/bin/sh\necho pwned > ../pwned\n", encoding="utf-8")
        text = self.replies.pop(0) if self.replies else ""
        return RunResult(
            self._name,
            self.code,
            text,
            self.exhausted,
            text,
            datetime(2026, 10, 26, tzinfo=UTC) if self.exhausted else None,
        )


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

    def pr_body(self) -> str:
        return next(
            stdin for argv, stdin in self.calls if argv[1:3] == ["pr", "create"]
        )


def team_run(
    tmp_path: Path,
    origin: Path,
    team: tuple[FakeModel, FakeModel, FakeModel],
    gh: FakeGh,
    *,
    triage: str | None = '{"proceed": true}',
) -> Run:
    planner, worker, reviewer = team
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    models: dict[str, Provider] = {}
    for model in (planner, worker, reviewer):
        models[model.name] = model
    return Run(
        issue=ISSUE,
        state=State.READY,
        run_id=3,
        team=Team(planner.name, worker.name, reviewer.name),
        providers=models,
        workspace=Workspace(root, subprocess_runner, GIT_ENV),
        writer=GitHubWriter(gh, {}, root),
        limits=Limits(max_files=5, max_lines=100),
        triage=None if triage is None else (lambda _system, _user: triage),
        ladder=DEFAULT_LADDER,
        remote=str(origin),
    )


def models(
    edits: list[dict[str, str | None]], reviews: list[str]
) -> tuple[FakeModel, FakeModel, FakeModel]:
    return (
        FakeModel("claude", ["1. Add docs/api.md"]),
        FakeModel("mimo", edits=edits),
        FakeModel("codex", reviews),
    )


def test_a_team_turns_an_issue_into_a_draft_pr(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"docs/api.md": "# API\n"}], [APPROVE])
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome == Outcome(State.REVIEW, "https://github.com/o/r/pull/9")
    assert gh.labels() == [
        ("agent:ready", "agent:running"),
        ("agent:running", "agent:review"),
    ]
    # Thinkers only read; the worker gets the plan and the untrusted issue.
    assert planner.prompts[0][1] is True
    assert reviewer.prompts[0][1] is True
    assert worker.prompts[0][1] is False
    assert "1. Add docs/api.md" in worker.prompts[0][0]
    assert "<<<UNTRUSTED_ISSUE>>>" in worker.prompts[0][0]
    assert "+# API" in reviewer.prompts[0][0]
    message = git(origin, "log", "-1", "--format=%B", "agent/7-run-3")
    assert message.startswith("docs: document the API\n\nCloses #7")
    assert "Agent-Planner: claude\nAgent-Worker: mimo\nAgent-Reviewer: codex" in message
    assert "Co-Authored-By: Claude" in message
    assert "Co-Authored-By: Codex" in message
    body = gh.pr_body()
    assert "**claude** planned, **mimo** implemented, **codex** reviewed" in body
    assert "Review 1 (codex, medium): Looks right." in body
    assert not (tmp_path / "work" / "pwned").exists()


def test_a_rejected_change_gets_one_repair_round(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models(
        [{"docs/api.md": "# APi\n"}, {"docs/api.md": "# API\n"}], [REJECT, APPROVE]
    )
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.REVIEW
    assert "- typo" in worker.prompts[1][0]
    assert "+# API" in reviewer.prompts[1][0]


def test_two_rejections_fail_with_the_findings(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models(
        [{"docs/api.md": "x\n"}, {"docs/api.md": "y\n"}], [REJECT, "not json"]
    )
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.FAILED
    assert "did not approve after 2 rounds" in outcome.detail
    assert gh.labels()[-1] == ("agent:running", "agent:failed")
    assert "Reply in the JSON format." in gh.comments()[0]


def test_protected_paths_are_refused_and_explained(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([{".github/workflows/ci.yml": "on: push\n"}], [])
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.FAILED
    assert "protected path: .github/workflows/ci.yml" in outcome.detail
    assert "protected path" in gh.comments()[0]
    assert reviewer.prompts == []
    assert "agent/7-run-3" not in git(origin, "branch", "--list")


def test_deleting_tests_is_refused(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"tests/test_a.py": None}], [])
    outcome = team_run(
        tmp_path, origin, (planner, worker, reviewer), FakeGh()
    ).execute()
    assert "test lines deleted" in outcome.detail


def test_no_change_fails(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([], [])
    outcome = team_run(
        tmp_path, origin, (planner, worker, reviewer), FakeGh()
    ).execute()
    assert outcome == Outcome(State.FAILED, "The worker made no changes.")


def test_triage_can_decline(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([], [])
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": false, "reason": "too vague"}',
    )
    assert run.execute() == Outcome(State.FAILED, "Triage declined: too vague")
    assert planner.prompts == []


def test_triage_failure_is_noted_and_skipped(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"a.md": "x\n"}], [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh(), triage=None)
    assert run.execute().state is State.REVIEW
    assert "Triage: skipped" in run.notes[0]

    def broken(_system: str, _user: str) -> str:
        raise httpx.ConnectError("down")

    planner, worker, reviewer = models([{"b.md": "x\n"}], [APPROVE])
    other = tmp_path / "second"
    other.mkdir()
    run = team_run(other, origin, (planner, worker, reviewer), FakeGh())
    run.triage = broken
    run.run_id = 4
    assert run.execute().state is State.REVIEW
    assert run.notes[0] == "Triage: unavailable (ConnectError)."


def test_the_planner_can_refuse(tmp_path: Path, origin: Path) -> None:
    planner = FakeModel("claude", ["REFUSE: asks to change CI"])
    team = (planner, FakeModel("mimo"), FakeModel("codex"))
    run = team_run(tmp_path, origin, team, FakeGh())
    assert run.execute() == Outcome(
        State.FAILED, "The planner refused: asks to change CI"
    )


def test_a_spent_worker_parks_the_task_and_names_itself(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([], [])
    worker.exhausted = True
    worker.code = 1
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.PARKED
    assert (outcome.parked, outcome.resets_at) == (
        "mimo",
        datetime(2026, 10, 26, tzinfo=UTC),
    )
    assert gh.labels()[-1] == ("agent:running", "agent:parked")


def test_a_failing_model_fails_the_run(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([], [])
    planner.code = 1
    outcome = team_run(
        tmp_path, origin, (planner, worker, reviewer), FakeGh()
    ).execute()
    assert outcome == Outcome(State.FAILED, "claude failed while planning.")


def test_a_patch_that_does_not_apply_fails(tmp_path: Path, origin: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    workspace = Workspace(root, subprocess_runner, GIT_ENV)
    clone = workspace.clone(str(origin), "elsewhere")
    with pytest.raises(GitError):
        workspace.apply(clone, "not a patch")


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


def test_json_reply_finds_the_object_or_nothing() -> None:
    assert json_reply('Sure! {"approve": true} done') == {"approve": True}
    assert json_reply("no json") is None
    assert json_reply("{broken") is None
    assert json_reply("{not json}") is None
    assert json_reply("[{}]") == {}


def test_run_live_records_and_parks_the_named_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    until = datetime(2026, 10, 26, tzinfo=UTC)

    class ParkedRun:
        def __init__(self, **_: object) -> None:
            pass

        def execute(self) -> Outcome:
            return Outcome(State.PARKED, "quota", "mimo", until)

    class Reader:
        def issue(self, repo: str, number: int) -> Issue:
            return Issue(repo, number, "t", "", ("agent:ready",), "owner")

    monkeypatch.setattr(cli, "Run", ParkedRun)
    queue = RunQueue(tmp_path / "q.sqlite3")
    plan = Plan([], [], Task("o/r", 1, "t"), TEAM)
    settings = Settings(work_root=tmp_path / "runs")
    with httpx.Client() as client:
        outcome = cli.run_live(plan, settings, Reader(), queue, client)  # pyright: ignore[reportArgumentType]
        assert outcome == Outcome(State.PARKED, "quota", "mimo", until)
        assert queue.parked_until() == {"mimo": until}
        empty = Plan([], [], None, None)
        assert cli.run_live(empty, settings, Reader(), queue, client) is None  # pyright: ignore[reportArgumentType]
    queue.close()


def test_quotas_cover_only_configured_providers() -> None:
    until = datetime(2026, 10, 26, tzinfo=UTC)
    quotas = cli.quotas(["claude", "codex", "mimo"], {"claude": until})
    assert quotas["claude"].exhausted
    assert quotas["claude"].resets_at == until
    assert "nvidia" not in quotas


def test_configured_and_adapters_follow_the_keys() -> None:
    bare = Settings(mimo_api_key=None, nvidia_api_key=None)
    assert cli.configured(bare) == ["claude", "codex"]
    assert set(cli.providers(bare)) == {"claude", "codex"}
    with httpx.Client() as client:
        assert cli.triage(bare, client) is None
        full = Settings(mimo_api_key=SecretStr("m"), nvidia_api_key=SecretStr("n"))
        assert cli.configured(full) == ["claude", "codex", "mimo", "nvidia"]
        adapters = cli.providers(full)
        assert set(adapters) == {"claude", "codex", "mimo", "nvidia"}
        claude = adapters["claude"]
        assert isinstance(claude, ClaudeCli)
        assert claude.work == Tuning("claude-sonnet-5-5", "medium")
        assert cli.triage(full, client) is not None
        only_nvidia = Settings(mimo_api_key=None, nvidia_api_key=SecretStr("n"))
        assert cli.triage(only_nvidia, client) is not None


def test_triage_rating_sets_the_thinking_level(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"a.md": "x\n"}], [APPROVE])
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    assert run.execute().state is State.REVIEW
    assert planner.tunings == [Tuning("claude-sonnet-5-5", "low")]
    assert reviewer.tunings == [Tuning("gpt-6.1-sol", "low")]
    assert worker.tunings == [None]
    assert run.notes[0] == "Triage: proceed, rated easy."


def test_a_planner_who_finds_it_harder_plans_again_on_opus(
    tmp_path: Path, origin: Path
) -> None:
    planner = FakeModel(
        "claude", ["DIFFICULTY: hard\n1. Quick plan", "DIFFICULTY: hard\n1. Deep plan"]
    )
    worker = FakeModel("mimo", edits=[{"a.md": "x\n"}])
    reviewer = FakeModel("codex", [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert planner.tunings == [
        Tuning("claude-sonnet-5-5", "medium"),
        Tuning("claude-opus-5-5", "high"),
    ]
    assert "1. Deep plan" in worker.prompts[0][0]
    assert "DIFFICULTY" not in worker.prompts[0][0]
    assert reviewer.tunings == [Tuning("gpt-6-astra", "high")]
    assert run.level is Level.HARD


def test_a_planner_who_finds_it_easier_lowers_the_review(
    tmp_path: Path, origin: Path
) -> None:
    planner = FakeModel("claude", ["DIFFICULTY: easy\n1. Tiny plan"])
    worker = FakeModel("mimo", edits=[{"a.md": "x\n"}])
    reviewer = FakeModel("codex", [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert len(planner.tunings) == 1
    assert reviewer.tunings == [Tuning("gpt-6.1-sol", "low")]
    assert "Planner rated it easy." in run.notes


def test_a_rejection_escalates_the_next_review(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models(
        [{"a.md": "x\n"}, {"a.md": "y\n"}], [REJECT, APPROVE]
    )
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert reviewer.tunings == [
        Tuning("gpt-6.1-sol", "medium"),
        Tuning("gpt-6-astra", "high"),
    ]
    assert "Rejected, so the next review runs at hard." in run.notes
    message = git(origin, "log", "-1", "--format=%B", "agent/7-run-3")
    assert "Agent-Level: hard" in message


def test_a_hard_task_stays_hard_after_a_rejection(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models(
        [{"a.md": "x\n"}, {"a.md": "y\n"}], [REJECT, APPROVE]
    )
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "hard"}',
    )
    assert run.execute().state is State.REVIEW
    assert reviewer.tunings == [Tuning("gpt-6-astra", "high")] * 2
    assert not any(note.startswith("Rejected") for note in run.notes)


def test_a_refusal_after_the_rating_line_is_still_a_refusal(
    tmp_path: Path, origin: Path
) -> None:
    planner = FakeModel("claude", ["DIFFICULTY: medium\nREFUSE: needs CI changes"])
    run = team_run(
        tmp_path, origin, (planner, FakeModel("mimo"), FakeModel("codex")), FakeGh()
    )
    assert run.execute() == Outcome(
        State.FAILED, "The planner refused: needs CI changes"
    )
