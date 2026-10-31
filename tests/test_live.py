"""Live team runs against real local git repositories, with fake models and gh."""

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from agent_dispatcher import cli, native_evidence
from agent_dispatcher.dispatcher import Plan
from agent_dispatcher.execute import Outcome, Provider, Run, Stop, json_reply, pr_title
from agent_dispatcher.gate import Limits
from agent_dispatcher.github import AgentPr, FailedCheck, GitHubReader
from agent_dispatcher.intake import Issue
from agent_dispatcher.levels import DEFAULT_LADDER, Level
from agent_dispatcher.opencode_worker import MiMoOpenCode
from agent_dispatcher.providers import (
    ClaudeCli,
    Runner,
    RunResult,
    Tuning,
    subprocess_runner,
)
from agent_dispatcher.publish import GitHubWriter, PublishError, error_lines
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.roles import Team
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import InvalidTransitionError, State
from agent_dispatcher.telemetry import Telemetry, sha256
from agent_dispatcher.workspace import GitError, Workspace

ISSUE = Issue(
    "o/r",
    7,
    "Document the API.",
    "Add docs",
    ("agent:ready", "documentation"),
    "owner",
    is_open=True,
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
        self.turns: list[int | None] = []
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
        max_turns: int | None = None,
    ) -> RunResult:
        self.prompts.append((prompt, read_only))
        self.tunings.append(tuning)
        self.turns.append(max_turns)
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
        authorize=lambda _issue, _expected: True,
    )


def models(
    edits: list[dict[str, str | None]], reviews: list[str]
) -> tuple[FakeModel, FakeModel, FakeModel]:
    return (
        FakeModel("claude", ["1. Add docs/api.md"]),
        FakeModel("mimo", edits=edits),
        FakeModel("codex", reviews),
    )


def test_native_call_identity_changes_with_task_candidate_stage_and_policy(
    tmp_path: Path,
    origin: Path,
) -> None:
    run = team_run(tmp_path, origin, models([], []), FakeGh())
    adapter = MiMoOpenCode(
        SecretStr("test-only"), checkpoint_root=tmp_path / "checkpoints"
    )
    run.providers = {"mimo": adapter}
    clone = run.workspace.clone(str(origin), "candidate")

    def bound() -> native_evidence.CallBinding:
        result = run._bound_provider("mimo", "implement", clone)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(result, MiMoOpenCode)
        assert result.binding is not None
        return result.binding

    before = bound()
    assert before.run_id == run.run_id
    assert before.base_commit == git(origin, "rev-parse", "HEAD").strip()
    assert adapter.binding is None  # Per-call binding cannot leak into later runs.
    (clone / "candidate.txt").write_text("new code")
    changed = bound()
    assert changed.patch_sha256 != before.patch_sha256
    assert changed.task_sha256 == before.task_sha256
    run.issue = replace(run.issue, body=run.issue.body + " changed")
    assert bound().task_sha256 != before.task_sha256
    run.limits = replace(run.limits, allow_test_deletions=True)
    assert bound().policy_sha256 != before.policy_sha256
    other_stage = run._bound_provider("mimo", "review", clone)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(other_stage, MiMoOpenCode)
    assert other_stage.binding is not None
    assert other_stage.binding.stage == "review"
    run.run_id += 1
    assert bound().run_id != before.run_id


@pytest.mark.parametrize(
    ("exit_code", "snapshot_failure"),
    [(0, False), (124, False), (0, True), (124, True), (429, True)],
)
def test_controller_call_retains_binding_before_native_process_starts(
    tmp_path: Path,
    origin: Path,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot_failure: bool,
) -> None:
    run = team_run(tmp_path, origin, models([], []), FakeGh())
    root = tmp_path / "checkpoints"
    run.telemetry = Telemetry(tmp_path / "telemetry")

    def runner(
        _argv: list[str],
        _cwd: Path,
        _stdin: str,
        env: Mapping[str, str],
    ) -> tuple[int, str, str]:
        profile = Path(env["XDG_DATA_HOME"]).parent
        metadata = json.loads((profile / "metadata.json").read_text())
        assert metadata["binding"]["run_id"] == run.run_id
        assert metadata["binding"]["stage"] == "implement"
        assert (
            metadata["binding"]["base_commit"]
            == git(origin, "rev-parse", "HEAD").strip()
        )
        assert (profile / "config.sha256").exists()
        (_cwd / "candidate.txt").write_text("partial work")
        if snapshot_failure:
            monkeypatch.setattr(
                run.workspace, "patch", Mock(side_effect=GitError("snapshot refused"))
            )
        return (
            exit_code,
            json.dumps({"type": "text", "part": {"text": "done"}}),
            ("quota exhausted" if exit_code == 429 else ""),
        )

    class Native(MiMoOpenCode):
        @override
        def run(
            self,
            prompt: str,
            worktree: Path,
            *,
            read_only: bool = False,
            tuning: Tuning | None = None,
            max_turns: int | None = None,
            runner: Runner = runner,
            environ: Mapping[str, str] | None = None,
        ) -> RunResult:
            return super().run(
                prompt,
                worktree,
                read_only=read_only,
                tuning=tuning,
                max_turns=max_turns,
                runner=runner,
                environ={} if environ is None else environ,
            )

    run.providers = {"mimo": Native(SecretStr("test-only"), checkpoint_root=root)}
    clone = run.workspace.clone(str(origin), "candidate")

    def call() -> str:
        return run._step(  # pyright: ignore[reportPrivateUsage]
            "mimo",
            "implementing",
            "private task",
            clone,
            read_only=False,
            kind="implement",
        )

    if exit_code == 429:
        with pytest.raises(Stop, match="quota exhausted") as stopped:
            call()
        assert stopped.value.outcome.state is State.PARKED
        assert stopped.value.outcome.parked == "mimo"
    elif exit_code == 124 and not snapshot_failure:
        # Retained timed-out edits continue to the gate and an informed reviewer.
        assert call() == "done"
        assert run.unfinished
    elif exit_code or snapshot_failure:
        message = "20-minute limit" if exit_code == 124 else "final candidate evidence"
        with pytest.raises(Stop, match=message):
            call()
    else:
        assert call() == "done"
    assert len(list(root.iterdir())) == 1
    profile = next(root.iterdir())
    candidate = json.loads((profile / "candidate.json").read_text())
    initial = json.loads((profile / "metadata.json").read_text())["binding"]
    if snapshot_failure:
        assert candidate["status"] == "unavailable"
        assert candidate["patch_sha256"] is None
    else:
        assert candidate["status"] == "captured"
        assert candidate["patch_sha256"] == sha256(run.workspace.patch(clone))
        assert candidate["patch_sha256"] != initial["patch_sha256"]
    assert json.loads((profile / "result.json").read_text())["exit_code"] == exit_code
    record = json.loads(next((tmp_path / "telemetry").glob("*.jsonl")).read_text())
    assert record["retention_errors"] == (
        ["snapshot_git_error"] if snapshot_failure else []
    )
    assert record["exit_code"] == (exit_code or int(snapshot_failure))


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
    assert "Plan SHA256:" in body
    assert "Looks right." not in body
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
        [{"docs/api.md": "x\n"}, {"docs/api.md": "y\n"}], [REJECT, REJECT]
    )
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.FAILED
    assert "did not approve after 2 rounds" in outcome.detail
    assert gh.labels()[-1] == ("agent:running", "agent:failed")
    assert "typo" in gh.comments()[0]


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
    long = Issue("o/r", 1, "Fix " + "x" * 200, "", ("bug",), "owner", is_open=True)
    assert pr_title(long).startswith("fix: fix x")
    assert len(pr_title(long)) <= 72


@pytest.mark.parametrize(
    "title", ["README claims drift", "MiMo times out", "X11 fails", "Add previews"]
)
def test_pr_title_subject_passes_the_fleet_title_rule(title: str) -> None:
    # The fleet pr-title check: subjectPattern ^(?![A-Z]).+$
    issue = Issue("o/r", 1, title, "", ("bug",), "owner", is_open=True)
    subject = pr_title(issue).split(": ", 1)[1]
    assert re.fullmatch(r"(?![A-Z]).+", subject)
    assert subject.count("`") % 2 == 0


@pytest.mark.parametrize(
    ("title", "labels", "expected"),
    [
        ("feat(api): support HEAD", (), "feat(api): support HEAD"),
        ("fix!: remove unsafe behavior", (), "fix!: remove unsafe behavior"),
        ("Add previews", ("enhancement",), "feat: add previews"),
        ("Explain preview", ("documentation",), "docs: explain preview"),
        ("do: strange header", (), "chore: do: strange header"),
        (
            "README claims nightly drift",
            ("documentation",),
            "docs: `README` claims nightly drift",
        ),
        ("MiMo times out", ("bug",), "fix: `MiMo` times out"),
        ("CI", (), "chore: `CI`"),
        ("X11 forwarding fails", ("bug",), "fix: `X11` forwarding fails"),
        ("A flaky test", ("bug",), "fix: a flaky test"),
        ("Read`ME is stale", (), "chore: `Read'ME` is stale"),
        ("L" + "ONG" * 20 + " name", (), ("chore: l" + "ONG" * 20 + " name")[:72]),
    ],
)
def test_pr_title_preserves_valid_intent_without_double_prefix(
    title: str,
    labels: tuple[str, ...],
    expected: str,
) -> None:
    assert (
        pr_title(Issue("o/r", 1, title, "", labels, "owner", is_open=True)) == expected
    )


def test_json_reply_finds_the_object_or_nothing() -> None:
    assert json_reply('Sure! {"approve": true} done') == {"approve": True}
    assert json_reply("no json") is None
    assert json_reply("{broken") is None
    assert json_reply("{not json}") is None
    assert json_reply("[{}]") == {}


def test_review_format_retry_is_read_only_not_a_code_repair(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([{"docs/api.md": "x\n"}], ["bad", APPROVE])
    gh = FakeGh()
    result = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert result.state is State.REVIEW
    assert len(worker.prompts) == 1
    assert len(reviewer.prompts) == 2
    assert all(read_only for _, read_only in reviewer.prompts)
    assert "exactly one valid JSON" in reviewer.prompts[1][0]


def test_two_invalid_review_verdicts_fail_without_publication(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([{"docs/api.md": "x\n"}], ["bad", "bad"])
    gh = FakeGh()
    result = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert result == Outcome(State.FAILED, "The reviewer returned invalid JSON twice.")
    assert len(worker.prompts) == 1
    assert len(reviewer.prompts) == 2
    assert not any(argv[1:3] == ["pr", "create"] for argv, _ in gh.calls)


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
            return Issue(repo, number, "t", "", ("agent:ready",), "owner", is_open=True)

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


def test_disabled_thinker_is_never_selected() -> None:
    settings = Settings(
        disabled_providers=["claude"],
        mimo_api_key=SecretStr("m"),
        nvidia_api_key=None,
    )
    available = cli.configured(settings)
    assert available == ["codex", "mimo"]
    team = cli.choose_team(cli.quotas(available, {}), 0.6, datetime.now(UTC))
    assert team is not None
    assert team.planner == team.reviewer == "codex"
    assert team.worker == "mimo"


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
    assert "PRIOR_PLAN" in planner.prompts[1][0]
    assert "1. Quick plan" in planner.prompts[1][0]
    assert "DIFFICULTY" not in worker.prompts[0][0]
    assert reviewer.tunings == [Tuning("gpt-6-astra", "high")]
    assert run.level is Level.HARD


@pytest.mark.parametrize(
    "reply", ["not JSON", "{}", '{"proceed":"true"}', '{"proceed":1}']
)
def test_malformed_triage_stops_before_model_work(
    tmp_path: Path, origin: Path, reply: str
):
    planner, worker, reviewer = models([], [])
    run = team_run(
        tmp_path, origin, (planner, worker, reviewer), FakeGh(), triage=reply
    )
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.FAILED
    assert not planner.prompts
    assert not worker.prompts
    assert not reviewer.prompts
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["outcome"] == "invalid_json"


@pytest.mark.parametrize(
    "reply", ["DIFFICULTY: hard\n", "DIFFICULTY: hard\nREFUSE: unsafe"]
)
def test_invalid_plan_never_refines_or_starts_work(
    tmp_path: Path, origin: Path, reply: str
):
    planner = FakeModel("claude", [reply])
    worker = FakeModel("mimo")
    reviewer = FakeModel("codex")
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.FAILED
    assert len(planner.prompts) == 1
    assert not worker.prompts
    assert not reviewer.prompts
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["outcome"] == (
        "refused" if "REFUSE:" in reply else "invalid_plan"
    )


def test_same_mimo_model_keeps_its_completed_harder_plan(tmp_path: Path, origin: Path):
    model = FakeModel(
        "mimo",
        ["DIFFICULTY: hard\n1. Reuse this plan", "implemented", APPROVE],
        [{"a.md": "x\n"}],
    )
    run = team_run(tmp_path, origin, (model, model, model), FakeGh())
    assert run.execute().state is State.REVIEW
    assert len(model.prompts) == 3
    assert "1. Reuse this plan" in model.prompts[1][0]
    assert model.tunings == [None, None, None]
    assert run.level is Level.HARD


def test_mimo_triage_requests_json_object_without_live_inference():
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"proceed":true}'}}]}
        )

    settings = Settings(mimo_api_key=SecretStr("fake"), nvidia_api_key=None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        screen = cli.triage(settings, client)
        assert screen is not None
        assert json.loads(screen("Reply as JSON", "task")) == {"proceed": True}


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


# CI repair of agent PRs.

REPAIR_AT = datetime(2026, 10, 26, tzinfo=UTC)


def published_pr(tmp_path: Path, origin: Path) -> AgentPr:
    """Run a team once so origin has an agent branch, and return it as a PR."""
    planner, worker, reviewer = models([{"docs/api.md": "# API\n"}], [APPROVE])
    first = tmp_path / "first"
    first.mkdir()
    team_run(first, origin, (planner, worker, reviewer), FakeGh()).execute()
    sha = git(origin, "rev-parse", "agent/7-run-3").strip()
    return AgentPr(number=9, branch="agent/7-run-3", sha=sha, issue=7)


def repair_run(tmp_path: Path, origin: Path, worker: FakeModel, reviewer: FakeModel):
    run = team_run(
        tmp_path, origin, (FakeModel("claude"), worker, reviewer), gh := FakeGh()
    )
    run.run_id = 4
    return run, gh


def test_a_ci_failure_is_repaired_on_the_same_branch(
    tmp_path: Path, origin: Path
) -> None:
    pr = published_pr(tmp_path, origin)
    worker = FakeModel("claude", edits=[{"docs/api.md": "# API\n\nFixed.\n"}])
    reviewer = FakeModel("codex", [APPROVE])
    run, gh = repair_run(tmp_path, origin, worker, reviewer)
    outcome = run.repair(pr, "E501 line too long")
    assert outcome.state is State.REVIEW
    assert "<<<CI_LOG>>>\nE501 line too long" in worker.prompts[0][0]
    assert "Fix the failing CI check" in reviewer.prompts[0][0]
    log = git(origin, "log", "--format=%s", "agent/7-run-3")
    assert log.splitlines()[0] == "fix: repair failing CI for #7"
    assert git(origin, "show", "agent/7-run-3:docs/api.md") == "# API\n\nFixed.\n"
    comment = next(stdin for argv, stdin in gh.calls if argv[1:3] == ["pr", "comment"])
    assert "pushed a CI repair" in comment
    assert gh.labels() == []


def test_a_rejected_repair_is_reported_and_not_pushed(
    tmp_path: Path, origin: Path
) -> None:
    pr = published_pr(tmp_path, origin)
    worker = FakeModel("claude", edits=[{"docs/api.md": "broken\n"}])
    run, gh = repair_run(tmp_path, origin, worker, FakeModel("codex", [REJECT]))
    outcome = run.repair(pr, "log")
    assert outcome.state is State.FAILED
    assert git(origin, "rev-parse", "agent/7-run-3").strip() == pr.sha
    comment = next(stdin for argv, stdin in gh.calls if argv[1:3] == ["pr", "comment"])
    assert "rejected the repair" in comment


def test_a_timed_out_ci_repair_with_edits_still_fails_closed(
    tmp_path: Path, origin: Path
) -> None:
    # CI repairs push to an open PR, so their time-limited edits are never kept.
    pr = published_pr(tmp_path, origin)
    worker = TimedOut("claude", edits=[{"docs/api.md": "# API\n\nHalf fixed.\n"}])
    reviewer = FakeModel("codex", [APPROVE])
    run, gh = repair_run(tmp_path, origin, worker, reviewer)
    outcome = run.repair(pr, "E501 line too long")
    assert outcome.state is State.FAILED
    assert "hit the 20-minute limit" in outcome.detail
    assert reviewer.prompts == []
    assert run.unfinished is False
    assert git(origin, "rev-parse", "agent/7-run-3").strip() == pr.sha
    comment = next(stdin for argv, stdin in gh.calls if argv[1:3] == ["pr", "comment"])
    assert "stopped" in comment


def test_a_spent_worker_parks_quietly(tmp_path: Path, origin: Path) -> None:
    pr = published_pr(tmp_path, origin)
    worker = FakeModel("claude")
    worker.exhausted = True
    worker.code = 1
    run, gh = repair_run(tmp_path, origin, worker, FakeModel("codex"))
    assert run.repair(pr, "log").state is State.PARKED
    assert gh.calls == []


def pulls_client(check_runs: Sequence[Mapping[str, object]]) -> httpx.Client:
    pulls = [
        {
            "number": 9,
            "head": {
                "ref": "agent/7-run-3",
                "sha": "abc",
                "repo": {"full_name": "o/r"},
            },
        },
        {
            "number": 10,
            "head": {"ref": "feature/x", "sha": "def", "repo": {"full_name": "o/r"}},
        },
        {"number": 11, "head": {"ref": "agent/8-run-4", "sha": "ghi", "repo": None}},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=pulls)
        return httpx.Response(200, json={"check_runs": check_runs})

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_reader_finds_only_agent_prs_from_this_repo() -> None:
    reader = GitHubReader(pulls_client([]))
    assert reader.agent_prs("o/r") == [AgentPr(9, "agent/7-run-3", "abc", 7)]


def test_failed_checks_wait_for_completion_and_skip_advisory_ones() -> None:
    url = "https://github.com/o/r/actions/runs/1/job/42"
    running = [{"name": "ci / Test", "status": "in_progress", "conclusion": None}]
    assert GitHubReader(pulls_client(running)).failed_checks("o/r", "abc") is None
    done = [
        {
            "name": "ci / Test",
            "status": "completed",
            "conclusion": "failure",
            "details_url": url,
        },
        {
            "name": "review / ai / Isolated analysis",
            "status": "completed",
            "conclusion": "failure",
        },
        {"name": "ci / Lint", "status": "completed", "conclusion": "success"},
    ]
    failed = GitHubReader(pulls_client(done)).failed_checks("o/r", "abc")
    assert failed == [FailedCheck("ci / Test", "42")]


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://github.com/o/r/actions/runs/41",
        "https://github.com/other/r/actions/runs/1/job/42",
        "https://ci.example.com/build/42",
        "https://github.com/o/r/actions/runs/1/job/42/extra",
    ],
)
def test_failed_checks_keep_only_this_repos_actions_job_ids(url: str | None) -> None:
    # The last part of any other link is not a job id: reading it as one would
    # fetch no log, or the wrong one.
    done = [
        {
            "name": "ci / Test",
            "status": "completed",
            "conclusion": "failure",
            "details_url": url,
        }
    ]
    failed = GitHubReader(pulls_client(done)).failed_checks("o/r", "abc")
    assert failed == [FailedCheck("ci / Test", "")]


def test_failed_log_keeps_the_tail_and_tolerates_errors(tmp_path: Path) -> None:
    class LogGh(FakeGh):
        def __call__(
            self, argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
        ) -> tuple[int, str, str]:
            if "--log-failed" in argv:
                return 0, "\n".join(f"line {n}" for n in range(200)), ""
            return super().__call__(argv, cwd, stdin, env)

    writer = GitHubWriter(LogGh(), {}, tmp_path)
    log = writer.failed_log("o/r", "42", lines=3)
    assert log == "line 197\nline 198\nline 199"
    broken = GitHubWriter(FakeGh(fail_on="--log-failed"), {}, tmp_path)
    assert broken.failed_log("o/r", "42") == ""


def test_queue_counts_repairs(tmp_path: Path) -> None:
    queue = RunQueue(tmp_path / "q.sqlite3")
    assert queue.repairs("o/r", 9) == 0
    queue.record_repair("o/r", 9, "abc", "REVIEW", REPAIR_AT)
    assert queue.repairs("o/r", 9) == 1
    queue.close()


class FakeReader:
    def __init__(self, failed: list[FailedCheck] | None) -> None:
        self.failed = failed

    def agent_prs(self, repo: str) -> list[AgentPr]:
        del repo
        return [AgentPr(9, "agent/7-run-3", "abc", 7)]

    def required_checks(self, repo: str, branch: str) -> set[str]:
        del repo, branch
        return {"ci / Test"}

    def failed_checks(
        self, repo: str, sha: str, required: set[str] | None = None
    ) -> list[FailedCheck] | None:
        del repo, sha
        assert required == {"ci / Test"}
        return self.failed

    def issue(self, repo: str, number: int) -> Issue:
        del repo, number
        return ISSUE


def test_repair_next_repairs_once_per_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repaired: list[tuple[int, str]] = []

    class FakeRun:
        def repair(self, pr: AgentPr, logs: str) -> cli.Outcome:
            repaired.append((pr.number, logs))
            return cli.Outcome(State.REVIEW, "fixed")

    class Writer:
        def __init__(self, *_: object) -> None:
            pass

        def failed_log(self, repo: str, job_id: str) -> str:
            del repo
            return f"log {job_id}"

    def fake_make_run(*_: object, **__: object) -> FakeRun:
        return FakeRun()

    monkeypatch.setattr(cli, "make_run", fake_make_run)
    monkeypatch.setattr(cli, "GitHubWriter", Writer)
    settings = Settings(repos=["o/r"], work_root=tmp_path / "runs", mimo_api_key=None)
    queue = RunQueue(tmp_path / "q.sqlite3")
    with httpx.Client() as client:
        reader = FakeReader([FailedCheck("ci / Test", "42")])
        result = cli.repair_next(settings, reader, queue, client)  # pyright: ignore[reportArgumentType]
        assert result == "repair: o/r#9 agent:review fixed"
        assert repaired == [(9, "### ci / Test\nlog 42")]
        assert cli.repair_next(settings, reader, queue, client) is None  # pyright: ignore[reportArgumentType]
        assert len(repaired) == 1
    queue.close()


def test_repair_next_waits_rather_than_repair_without_any_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    read: list[str] = []

    class Writer:
        def __init__(self, *_: object) -> None:
            pass

        def failed_log(self, repo: str, job_id: str) -> str:
            del repo
            read.append(job_id)
            return "boom" if job_id == "7" else ""

    def no_run(*_: object, **__: object) -> object:
        raise AssertionError

    monkeypatch.setattr(cli, "make_run", no_run)
    monkeypatch.setattr(cli, "GitHubWriter", Writer)
    settings = Settings(repos=["o/r"], work_root=tmp_path / "runs", mimo_api_key=None)
    queue = RunQueue(tmp_path / "q.sqlite3")
    with httpx.Client() as client:
        # No job link, and a job whose log can't be read: nothing to repair from.
        reader = FakeReader(
            [FailedCheck("ci / Test", ""), FailedCheck("ci / Lint", "8")]
        )
        assert cli.repair_next(settings, reader, queue, client) is None  # pyright: ignore[reportArgumentType]
        assert read == ["8"]
        assert queue.repairs("o/r", 9) == 0
        # The wait is said in the cycle log, so it can't look like nothing failed.
        assert capsys.readouterr().err == (
            "repair: o/r#9 waits, no readable log for ci / Test, ci / Lint\n"
        )

        repaired: list[str] = []

        class FakeRun:
            def repair(self, pr: AgentPr, logs: str) -> cli.Outcome:
                del pr
                repaired.append(logs)
                return cli.Outcome(State.REVIEW, "fixed")

        def fake_make_run(*_: object, **__: object) -> FakeRun:
            return FakeRun()

        monkeypatch.setattr(cli, "make_run", fake_make_run)
        # Checks without a job link don't use up the three logs that are read.
        unlinked = [FailedCheck(f"ext / {n}", "") for n in range(3)]
        reader = FakeReader(
            [*unlinked, FailedCheck("ci / Lint", "8"), FailedCheck("ci / Test", "7")]
        )
        assert cli.repair_next(settings, reader, queue, client) is not None  # pyright: ignore[reportArgumentType]
        # Only the readable log goes to the agent; an empty section adds nothing.
        assert repaired == ["### ci / Test\nboom"]
    queue.close()


def test_repair_next_skips_green_or_running_prs_and_parked_teams(
    tmp_path: Path,
) -> None:
    settings = Settings(repos=["o/r"], work_root=tmp_path / "runs", mimo_api_key=None)
    queue = RunQueue(tmp_path / "q.sqlite3")
    with httpx.Client() as client:
        nothing_failed: tuple[list[FailedCheck] | None, ...] = (None, [])
        for failed in nothing_failed:
            reader = FakeReader(failed)
            assert cli.repair_next(settings, reader, queue, client) is None  # pyright: ignore[reportArgumentType]
        until = datetime(2099, 1, 1, tzinfo=UTC)
        queue.park("claude", until)
        queue.park("codex", until)
        reader = FakeReader([FailedCheck("ci / Test", "42")])
        assert cli.repair_next(settings, reader, queue, client) is None  # pyright: ignore[reportArgumentType]
    queue.close()


def test_every_attempt_is_recorded_without_prompt_text(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models(
        [{"docs/api.md": "# API\n"}, {"docs/api.md": "# API!\n"}], [REJECT, APPROVE]
    )
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.triage_model = "mimo-v2.6-flash"
    assert run.execute().state is State.REVIEW
    files = list((tmp_path / "telemetry").glob("attempts-*.jsonl"))
    text = files[0].read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [(r["stage"], r["outcome"]) for r in records] == [
        ("triage", "proceed"),
        ("plan", "ok"),
        ("implement", "ok"),
        ("gate", "passed"),
        ("review", "rejected"),
        ("repair", "ok"),
        ("gate", "passed"),
        ("review", "approved"),
        ("publish", "ok"),
    ]
    assert [r["attempt"] for r in records] == list(range(1, 10))
    assert {r["run_id"] for r in records} == {3}
    assert records[0]["model"] == "mimo-v2.6-flash"
    assert records[1]["level"] == "easy"
    assert records[7]["level"] == "medium"
    assert records[3]["files"] == ["docs/api.md"]
    assert set(records[4]["context_sha256"]) == {"issue", "plan", "diff"}
    assert records[8]["pr_url"] == "https://github.com/o/r/pull/9"
    assert all(r["available_usage"] is None for r in records)
    assert "<<<UNTRUSTED_ISSUE>>>" not in text
    assert "You are implementing one GitHub issue" not in text
    assert "You are reviewing a change" not in text


def test_failures_and_quota_are_recorded(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([], [])
    worker.exhausted = True
    worker.code = 1
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh(), triage=None)
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.PARKED
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(r["stage"], r["outcome"]) for r in records] == [
        ("triage", "skipped"),
        ("plan", "ok"),
        ("implement", "exhausted"),
    ]
    assert records[2]["exhausted"] is True
    assert records[2]["resets_at"] == "2026-10-26T00:00:00+00:00"


def test_refusals_and_declines_are_recorded(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{".github/workflows/ci.yml": "on: push\n"}], [])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.execute()
    refusing = FakeModel("claude", ["REFUSE: no"])
    second = tmp_path / "second"
    second.mkdir()
    refused = team_run(
        second,
        origin,
        (refusing, FakeModel("mimo"), FakeModel("codex")),
        FakeGh(),
        triage='{"proceed": false, "reason": "vague"}',
    )
    refused.telemetry = Telemetry(tmp_path / "telemetry")
    refused.run_id = 4
    refused.execute()
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    gate = next(r for r in records if r["stage"] == "gate")
    assert gate["outcome"] == "refused"
    assert gate["gate_refusals"] == ["protected path: .github/workflows/ci.yml"]
    assert records[-1]["stage"] == "triage"
    assert records[-1]["outcome"] == "declined"


def test_routing_records_name_each_left_out_provider(tmp_path: Path) -> None:
    settings = Settings(
        work_root=tmp_path / "runs", mimo_api_key=SecretStr("m"), nvidia_api_key=None
    )
    queue = RunQueue(tmp_path / "q.sqlite3")
    until = datetime(2026, 10, 26, tzinfo=UTC)
    queue.park("codex", until)
    cli.record_routing(
        settings, queue, Team("claude", "mimo", "claude"), 5, Task("o/r", 1, "t")
    )
    queue.close()
    records = [
        json.loads(line)
        for line in next((tmp_path / "runs" / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(r["provider"], r["reason"]) for r in records] == [
        ("codex", "parked"),
        ("nvidia", "not configured"),
    ]
    assert records[0]["resets_at"] == until.isoformat()
    assert all(r["stage"] == "route" and r["outcome"] == "skipped" for r in records)


def test_triage_model_follows_the_keys() -> None:
    assert cli.triage_model(Settings(mimo_api_key=SecretStr("m"))) == "mimo-v2.6-flash"
    assert (
        cli.triage_model(Settings(mimo_api_key=None, nvidia_api_key=SecretStr("n")))
        == "z-ai/glm-5.3-flash"
    )
    assert cli.triage_model(Settings(mimo_api_key=None, nvidia_api_key=None)) is None


def rules_client(status: int, rules: list[dict[str, object]]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rules/branches/main")
        return httpx.Response(status, json=rules)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_required_checks_come_from_the_ruleset() -> None:
    rules: list[dict[str, object]] = [
        {"type": "pull_request", "parameters": {}},
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "ci / Test"},
                    {"context": "pr-title / Conventional title"},
                ]
            },
        },
    ]
    reader = GitHubReader(rules_client(200, rules))
    assert reader.required_checks("o/r", "main") == {
        "ci / Test",
        "pr-title / Conventional title",
    }
    assert GitHubReader(rules_client(404, [])).required_checks("o/r", "main") == set()


def test_only_required_failures_are_repaired() -> None:
    done = [
        {
            "name": "ci / Test",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/o/r/actions/runs/5/job/1",
        },
        {
            "name": "lint / Lint (all languages)",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/o/r/actions/runs/5/job/2",
        },
    ]
    reader = GitHubReader(pulls_client(done))
    assert reader.failed_checks("o/r", "abc", {"ci / Test"}) == [
        FailedCheck("ci / Test", "1")
    ]
    assert reader.failed_checks("o/r", "abc", set()) == []


class Crashing(FakeModel):
    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        del prompt, worktree, read_only, tuning, max_turns
        message = "boom"
        raise RuntimeError(message)


def test_an_unexpected_error_still_ends_in_a_final_state(
    tmp_path: Path, origin: Path
) -> None:
    gh = FakeGh()
    run = team_run(
        tmp_path,
        origin,
        (Crashing("claude"), FakeModel("mimo"), FakeModel("codex")),
        gh,
    )
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute() == Outcome(State.FAILED, "Controller error: RuntimeError.")
    assert gh.labels()[-1] == ("agent:running", "agent:failed")
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert (records[-1]["stage"], records[-1]["reason"]) == ("error", "RuntimeError")
    pr = published_pr(tmp_path, origin)
    (tmp_path / "work2").mkdir()
    repair = team_run(
        tmp_path / "work2",
        origin,
        (Crashing("claude"), FakeModel("mimo"), FakeModel("codex")),
        gh2 := FakeGh(),
    )
    repair.run_id = 6
    assert repair.repair(pr, "log") == Outcome(
        State.FAILED, "Controller error: RuntimeError."
    )
    assert any(argv[1:3] == ["pr", "comment"] for argv, _ in gh2.calls)


def test_turn_caps_follow_the_level(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"a.md": "x\n"}], [APPROVE])
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    assert run.execute().state is State.REVIEW
    assert (planner.turns, worker.turns, reviewer.turns) == ([10], [15], [10])


def test_easy_docs_can_skip_the_planner(tmp_path: Path, origin: Path) -> None:
    planner, worker, reviewer = models([{"a.md": "x\n"}], [APPROVE])
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    run.skip_easy_docs_plan = True
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.REVIEW
    assert planner.prompts == []
    assert "No separate plan" in worker.prompts[0][0]
    assert "Plan: skipped for an easy documentation task." in run.notes
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert ("plan", "skipped") in [(r["stage"], r["outcome"]) for r in records]


def test_skipping_the_plan_needs_easy_and_documentation(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([{"a.md": "x\n"}], [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.skip_easy_docs_plan = True
    assert run.execute().state is State.REVIEW
    assert len(planner.prompts) == 1


class TurnLimited(FakeModel):
    """Hits its turn cap the first `limits` times, then finishes normally."""

    def __init__(self, name: str, limits: int, **kwargs: object) -> None:
        super().__init__(name, **kwargs)  # pyright: ignore[reportArgumentType]
        self.limits = limits

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        result = super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )
        if self.limits:
            self.limits -= 1
            return replace(result, exit_code=1, stop_reason="max_turns")
        return result


def test_running_out_of_turns_raises_the_level_and_continues(
    tmp_path: Path, origin: Path
) -> None:
    worker = TurnLimited("mimo", 1, edits=[{"a.md": "x\n"}, {"b.md": "y\n"}])
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex", [APPROVE])
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.REVIEW
    assert worker.turns == [15, 25]
    assert run.level is Level.MEDIUM
    assert (
        "mimo ran out of turns while implementing, so it continued at medium."
        in run.notes
    )
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    outcomes = [(r["stage"], r["outcome"]) for r in records]
    assert ("implement", "turns_exhausted") in outcomes
    assert ("implement", "ok") in outcomes


def test_out_of_turns_at_hard_fails_with_the_reason(
    tmp_path: Path, origin: Path
) -> None:
    worker = TurnLimited("mimo", 5, edits=[{"a.md": "x\n"}] * 5)
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex")
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "hard"}',
    )
    outcome = run.execute()
    assert outcome == Outcome(
        State.FAILED,
        "mimo failed while implementing (it ran out of turns even at the hard level).",
    )
    assert worker.turns == [40]


class TimedOut(FakeModel):
    """Edits as asked, then reports the call time limit on every non-read-only call."""

    def __init__(
        self, name: str, retention_errors: tuple[str, ...] = (), **kwargs: object
    ) -> None:
        super().__init__(name, **kwargs)  # pyright: ignore[reportArgumentType]
        self.retention_errors = retention_errors

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        result = super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )
        if read_only:
            return result
        return replace(
            result,
            exit_code=124,
            stop_reason="timed_out",
            retention_errors=self.retention_errors,
        )


def test_timed_out_edits_go_to_the_gate_and_an_informed_reviewer(
    tmp_path: Path, origin: Path
) -> None:
    worker = TimedOut("mimo", edits=[{"docs/api.md": "# API\n"}])
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex", [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    run.telemetry = Telemetry(tmp_path / "telemetry")
    assert run.execute().state is State.REVIEW
    review_prompt, read_only = reviewer.prompts[0]
    assert read_only
    assert "may be\nunfinished" in review_prompt
    assert any("hit the call time limit while implementing" in n for n in run.notes)
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert ("implement", "timed_out") in [(r["stage"], r["outcome"]) for r in records]


def test_kept_timed_out_edits_are_still_refused_by_the_gate(
    tmp_path: Path, origin: Path
) -> None:
    worker = TimedOut("mimo", edits=[{".github/workflows/ci.yml": "on: push\n"}])
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex", [APPROVE])
    gh = FakeGh()
    outcome = team_run(tmp_path, origin, (planner, worker, reviewer), gh).execute()
    assert outcome.state is State.FAILED
    assert "protected path: .github/workflows/ci.yml" in outcome.detail
    assert reviewer.prompts == []
    assert "agent/7-run-3" not in git(origin, "branch", "--list")


def test_a_timeout_without_edits_still_fails(tmp_path: Path, origin: Path) -> None:
    worker = TimedOut("mimo")
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex")
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute() == Outcome(
        State.FAILED,
        "mimo failed while implementing (it hit the 20-minute limit for one call).",
    )
    assert reviewer.prompts == []


def test_a_timeout_that_lost_its_evidence_still_fails(
    tmp_path: Path, origin: Path
) -> None:
    worker = TimedOut(
        "mimo",
        retention_errors=("receipt_storage_error",),
        edits=[{"docs/api.md": "# API\n"}],
    )
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex")
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.FAILED
    assert reviewer.prompts == []


class TimesOutOnRepair(FakeModel):
    """Finishes its implement call, then reports the time limit on the repair."""

    def run(
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        repairing = "A reviewer found these problems" in prompt
        result = super().run(
            prompt, worktree, read_only=read_only, tuning=tuning, max_turns=max_turns
        )
        if repairing:
            return replace(result, exit_code=124, stop_reason="timed_out")
        return result


def test_a_repair_timeout_that_changed_nothing_still_fails(
    tmp_path: Path, origin: Path
) -> None:
    # The implement diff is still on disk; the repair call itself made no edits.
    worker = TimesOutOnRepair("mimo", edits=[{"docs/api.md": "# API\n"}])
    planner, reviewer = FakeModel("claude", ["1. plan"]), FakeModel("codex", [REJECT])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute() == Outcome(
        State.FAILED,
        "mimo failed while implementing (it hit the 20-minute limit for one call).",
    )
    assert len(reviewer.prompts) == 1


def test_a_repair_timeout_with_new_edits_goes_to_an_informed_review(
    tmp_path: Path, origin: Path
) -> None:
    worker = TimesOutOnRepair(
        "mimo", edits=[{"docs/api.md": "# API\n"}, {"docs/api.md": "# API v2\n"}]
    )
    planner = FakeModel("claude", ["1. plan"])
    reviewer = FakeModel("codex", [REJECT, APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert "unfinished" not in reviewer.prompts[0][0]
    assert "may be\nunfinished" in reviewer.prompts[1][0]


def test_a_later_finished_call_clears_the_unfinished_warning(
    tmp_path: Path, origin: Path
) -> None:
    class TimesOutFirst(FakeModel):
        def run(
            self,
            prompt: str,
            worktree: Path,
            *,
            read_only: bool = False,
            tuning: Tuning | None = None,
            max_turns: int | None = None,
        ) -> RunResult:
            first = not self.prompts
            result = super().run(
                prompt,
                worktree,
                read_only=read_only,
                tuning=tuning,
                max_turns=max_turns,
            )
            return (
                replace(result, exit_code=124, stop_reason="timed_out")
                if first
                else result
            )

    worker = TimesOutFirst(
        "mimo", edits=[{"docs/api.md": "# API\n"}, {"docs/api.md": "# API v2\n"}]
    )
    planner = FakeModel("claude", ["1. plan"])
    reviewer = FakeModel("codex", [REJECT, APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert "may be\nunfinished" in reviewer.prompts[0][0]
    assert "unfinished" not in reviewer.prompts[1][0]
    assert not run.unfinished
    assert any("no longer get the possibly-unfinished warning" in n for n in run.notes)


def test_a_finished_worker_gets_no_unfinished_warning(
    tmp_path: Path, origin: Path
) -> None:
    planner, worker, reviewer = models([{"docs/api.md": "# API\n"}], [APPROVE])
    run = team_run(tmp_path, origin, (planner, worker, reviewer), FakeGh())
    assert run.execute().state is State.REVIEW
    assert "unfinished" not in reviewer.prompts[0][0]


def test_read_only_planner_turn_cap_does_not_restart_its_conversation(
    tmp_path: Path,
    origin: Path,
) -> None:
    planner = TurnLimited("claude", 3)
    worker, reviewer = FakeModel("mimo"), FakeModel("codex")
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    outcome = run.execute()
    assert outcome.state is State.FAILED
    assert "no restart escalation" in outcome.detail
    assert planner.turns == [10]
    assert worker.prompts == []
    assert reviewer.prompts == []


def test_read_only_reviewer_turn_cap_does_not_restart_or_authorize_repairs(
    tmp_path: Path,
    origin: Path,
) -> None:
    planner = FakeModel("claude", ["1. plan"])
    worker = FakeModel("mimo", edits=[{"a.md": "change\n"}])
    reviewer = TurnLimited("codex", 3)
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        FakeGh(),
        triage='{"proceed": true, "difficulty": "easy"}',
    )
    assert run.execute().state is State.FAILED
    assert reviewer.turns == [10]
    assert len(worker.prompts) == 1


def test_credential_in_worker_patch_is_refused_before_push(
    tmp_path: Path,
    origin: Path,
) -> None:
    sensitive = "held-controller-secret-value"
    planner, worker, reviewer = models([{"a.md": sensitive}], [APPROVE])
    gh = FakeGh()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), gh)
    run.telemetry = Telemetry(tmp_path / "telemetry", secrets=[sensitive])
    assert run.execute().state is State.FAILED
    assert reviewer.prompts == []
    assert not any(argv[1:3] == ["pr", "create"] for argv, _ in gh.calls)
    assert sensitive not in str(gh.calls)


def test_raw_plan_and_reviewer_text_do_not_enter_public_pr(
    tmp_path: Path,
    origin: Path,
) -> None:
    sensitive = "held-controller-secret-value"
    planner = FakeModel("claude", ["1. Plan with " + sensitive])
    worker = FakeModel("mimo", edits=[{"a.md": "safe change\n"}])
    reviewer = FakeModel("codex", [json.dumps({"approve": True, "summary": sensitive})])
    gh = FakeGh()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), gh)
    run.telemetry = Telemetry(tmp_path / "telemetry", secrets=[sensitive])
    assert run.execute().state is State.REVIEW
    assert sensitive not in gh.pr_body()
    assert "1. Plan with" not in gh.pr_body()


def test_declined_model_reason_is_redacted_before_issue_comment(
    tmp_path: Path,
    origin: Path,
) -> None:
    sensitive = "held-controller-secret-value"
    planner, worker, reviewer = models([], [])
    gh = FakeGh()
    run = team_run(
        tmp_path,
        origin,
        (planner, worker, reviewer),
        gh,
        triage=json.dumps({"proceed": False, "reason": sensitive}),
    )
    run.telemetry = Telemetry(tmp_path / "telemetry", secrets=[sensitive])
    outcome = run.execute()
    assert outcome.state is State.FAILED
    assert sensitive not in outcome.detail
    assert sensitive not in str(gh.calls)
    assert "[REDACTED]" in outcome.detail


def test_repair_does_not_wait_for_advisory_or_optional_checks() -> None:
    runs = [
        {
            "name": "ci / Types",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.com/o/r/actions/runs/5/job/7",
        },
        {
            "name": "review / ai / Isolated analysis",
            "status": "in_progress",
            "conclusion": None,
        },
        {"name": "PR-Agent (advisory trial)", "status": "queued", "conclusion": None},
    ]
    reader = GitHubReader(pulls_client(runs))
    assert reader.failed_checks("o/r", "abc", {"ci / Types"}) == [
        FailedCheck("ci / Types", "7")
    ]
    still_running = [
        *runs,
        {"name": "ci / Test", "status": "in_progress", "conclusion": None},
    ]
    assert (
        GitHubReader(pulls_client(still_running)).failed_checks(
            "o/r", "abc", {"ci / Types", "ci / Test"}
        )
        is None
    )


TYPES = "ci / Types\tUNKNOWN STEP\t2026-10-26T11:19:04.6252790Z "
LINT = "ci / Lint\tUNKNOWN STEP\t2026-10-26T11:19:01.1Z "
REAL_FAILED_LOG = "\n".join(
    [
        TYPES + "  /w/tests/test_api.py:172:23 - error: Argument of type bytes",
        TYPES + "4 errors, 0 warnings",
        TYPES + "##[error]Process completed with exit code 1.",
        LINT + "##[error]tests/test_api.py:165:11: N818 Exception name `Custom`",
        TYPES + "Oct 26 runner agentservice[2044]: INFO Listening module=armour",
        TYPES + "Cleaning up orphan processes",
    ]
)


def test_error_lines_keep_the_failure_not_the_runner_noise() -> None:
    lines = error_lines(REAL_FAILED_LOG).splitlines()
    assert lines[0] == "  /w/tests/test_api.py:172:23 - error: Argument of type bytes"
    assert "N818" in lines[3]
    assert not any("agentservice" in line or "orphan" in line for line in lines)
    assert all("UNKNOWN STEP" not in line for line in lines)


def test_error_lines_fall_back_to_the_cleaned_tail() -> None:
    stamp = "job\tstep\t2026-10-26T11:19:06.2Z "
    log = f"{stamp}some output\n{stamp}more"
    assert error_lines(log, limit=1) == "more"


def test_error_lines_keep_spelling_details() -> None:
    log = "tests/test_api.py:7: typo ==> correction\nCleaning up orphan processes"
    assert error_lines(log) == "tests/test_api.py:7: typo ==> correction"


@pytest.mark.parametrize("failure", ["revoked", "unavailable", "missing_adapter"])
def test_publication_authority_refusal_never_pushes_or_comments(
    tmp_path: Path, origin: Path, failure: str
):
    planner, worker, reviewer = models([{"docs/api.md": "# API\n"}], [APPROVE])
    gh = FakeGh()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), gh)
    checks: list[State] = []

    def authorize(_issue: Issue, expected: State) -> bool:
        checks.append(expected)
        if len(checks) == 1:
            return True
        if failure == "unavailable":
            raise OSError
        return False

    run.telemetry = Telemetry(tmp_path / "telemetry")
    run.authorize = None if failure == "missing_adapter" else authorize
    assert run.execute().state is State.FAILED
    records = [
        json.loads(line)
        for line in next((tmp_path / "telemetry").glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    decision = native_evidence.metrics(records[-1])
    assert decision["stage"] == "authorization"
    assert decision["outcome"] == ("refused" if failure == "revoked" else "unavailable")
    assert not any(argv[1:3] == ["pr", "create"] for argv, _ in gh.calls)
    assert not gh.comments()
    assert git(origin, "for-each-ref", "refs/heads/agent/") == ""
    if failure == "missing_adapter":
        assert not gh.calls
        assert not planner.prompts
    else:
        assert checks == [State.READY, State.RUNNING]
        assert gh.labels() == [("agent:ready", "agent:running")]


def test_authority_is_rechecked_between_push_and_draft_pr(tmp_path: Path, origin: Path):
    planner, worker, reviewer = models([{"docs/api.md": "# API\n"}], [APPROVE])
    gh = FakeGh()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), gh)
    checks: list[State] = []

    def authorize(_issue: Issue, expected: State) -> bool:
        checks.append(expected)
        return len(checks) < 3

    run.authorize = authorize
    assert run.execute().state is State.FAILED
    assert checks == [State.READY, State.RUNNING, State.RUNNING]
    assert not any(argv[1:3] == ["pr", "create"] for argv, _ in gh.calls)
    assert not gh.comments()
    assert git(origin, "for-each-ref", "refs/heads/agent/")


@pytest.mark.parametrize("deny_at", [1, 2])
def test_repair_authority_refusal_preserves_branch_and_writes_nothing(
    tmp_path: Path, origin: Path, deny_at: int
):
    pr = published_pr(tmp_path, origin)
    before = git(origin, "rev-parse", pr.branch)
    worker = FakeModel("claude", edits=[{"docs/api.md": "# Fixed\n"}])
    run, gh = repair_run(tmp_path, origin, worker, FakeModel("codex", [APPROVE]))
    checks: list[State] = []

    def authorize(_issue: Issue, expected: State) -> bool:
        checks.append(expected)
        return len(checks) < deny_at

    run.authorize = authorize
    assert run.repair(pr, "Synthetic CI error").state is State.FAILED
    assert checks == [State.REVIEW] * deny_at
    assert git(origin, "rev-parse", pr.branch) == before
    assert not gh.calls


def test_revoked_task_skips_quota_cleanup_but_preserves_provider_park(
    tmp_path: Path, origin: Path
):
    planner, worker, reviewer = models([], [])
    worker.exhausted = True
    gh = FakeGh()
    run = team_run(tmp_path, origin, (planner, worker, reviewer), gh)
    checks: list[State] = []

    def authorize(_issue: Issue, expected: State) -> bool:
        checks.append(expected)
        return len(checks) == 1

    run.authorize = authorize
    outcome = run.execute()
    assert outcome.state is State.PARKED
    assert outcome.parked == "mimo"
    assert outcome.resets_at == datetime(2026, 10, 26, tzinfo=UTC)
    assert checks == [State.READY, State.RUNNING]
    assert gh.labels() == [("agent:ready", "agent:running")]
    assert not gh.comments()
