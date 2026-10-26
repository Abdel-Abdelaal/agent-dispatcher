from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from agent_dispatcher import cli
from agent_dispatcher.dispatcher import Cycle, Plan, choose_team
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.intake import Issue
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.quota import Quota
from agent_dispatcher.settings import Settings

REAL_CLIENT = httpx.Client
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
OPEN = {"claude": Quota("claude", 0.1, None), "codex": Quota("codex", 0.1, None)}


def test_teams_follow_quota() -> None:
    open_all = {name: Quota(name, 0.1, None) for name in ("claude", "codex", "mimo")}
    team = choose_team(open_all, 0.6, NOW)
    assert team is not None
    assert (team.planner, team.worker, team.reviewer) == ("claude", "mimo", "codex")
    claude_spent = {**open_all, "claude": Quota("claude", 0.7, None)}
    solo = choose_team(claude_spent, 0.6, NOW)
    assert solo is not None
    assert (solo.planner, solo.reviewer) == ("codex", "codex")
    thinkers_spent = {"mimo": Quota("mimo", 0.1, None)}
    assert choose_team(thinkers_spent, 0.6, NOW) is None


def test_cycle_queues_only_owner_labelled_issues(tmp_path: Path) -> None:
    issues = [
        Issue("o/r", 1, "mine", "", ("agent:ready",), "owner"),
        Issue("o/r", 2, "theirs", "", ("agent:ready",), "stranger"),
    ]
    queue = RunQueue(tmp_path / "q.sqlite3")
    cycle = Cycle("owner", ["o/r"], lambda _repo: issues, queue, 0.6)
    plan = cycle.plan(OPEN, NOW)
    assert [issue.number for issue in plan.queued] == [1]
    assert [issue.number for issue in plan.ignored] == [2]
    assert plan.next_task is not None
    assert plan.team is not None
    assert (plan.next_task.number, plan.team.planner) == (1, "claude")
    assert cycle.plan(OPEN, NOW).queued == []
    queue.close()


def github_client() -> httpx.Client:
    issues: list[dict[str, object]] = [
        {
            "number": 7,
            "title": "Add test",
            "body": None,
            "labels": [{"name": "agent:ready"}],
        },
        {"number": 8, "title": "A PR", "labels": [], "pull_request": {}},
    ]
    events = [
        {"event": "labeled", "label": {"name": "agent:ready"}, "actor": {"login": "x"}},
        {"event": "labeled", "label": {"name": "bug"}, "actor": {"login": "y"}},
        {
            "event": "labeled",
            "label": {"name": "agent:ready"},
            "actor": {"login": "owner"},
        },
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        if request.url.path.endswith("/issues/7"):
            return httpx.Response(200, json=issues[0])
        return httpx.Response(200, json=issues)

    return REAL_CLIENT(transport=httpx.MockTransport(handle))


def test_reader_skips_prs_and_uses_the_latest_ready_labeller() -> None:
    [issue] = GitHubReader(github_client(), "t").ready_issues("o/r")
    assert (issue.number, issue.body, issue.labelled_by) == (7, "", "owner")


def test_cli_dry_run_prints_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISPATCHER_OWNER", "owner")
    monkeypatch.setenv("DISPATCHER_REPOS", "o/r, ")
    monkeypatch.setenv("DISPATCHER_MIMO_API_KEY", "")
    monkeypatch.setenv("DISPATCHER_NVIDIA_API_KEY", "")
    monkeypatch.setenv("DISPATCHER_DB_PATH", str(tmp_path / "q.sqlite3"))
    monkeypatch.setattr(cli.httpx, "Client", github_client)
    assert cli.main(["once", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "queued: o/r#7",
        "next: o/r#7: claude plans, claude implements, codex reviews (dry run)",
    ]


def test_describe_parked_and_empty() -> None:
    issue = Issue("o/r", 3, "t", "", ("agent:ready",), "stranger")
    plan = Plan([], [issue], None, None)
    assert cli.describe(plan, dry_run=True).splitlines() == [
        "ignored (ready label not added by the owner): o/r#3",
        "next: nothing queued",
    ]
    parked = Plan([], [], Task("o/r", 4, "t"), None)
    assert cli.describe(parked, dry_run=True) == "next: o/r#4 parked"


def test_settings_accept_a_list_or_a_comma_string() -> None:
    assert Settings(repos=["a/b"]).repos == ["a/b"]
    assert Settings(repos="a/b, c/d").repos == ["a/b", "c/d"]  # pyright: ignore[reportArgumentType]


def test_reader_fetches_one_issue_fresh() -> None:
    issue = GitHubReader(github_client()).issue("o/r", 7)
    assert (issue.title, issue.labelled_by) == ("Add test", "owner")


def test_cli_live_cycle_runs_the_planned_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISPATCHER_OWNER", "owner")
    monkeypatch.setenv("DISPATCHER_REPOS", "o/r")
    monkeypatch.setenv("DISPATCHER_DB_PATH", str(tmp_path / "q.sqlite3"))
    monkeypatch.setattr(cli.httpx, "Client", github_client)

    def fake_run_live(*_: object) -> cli.Outcome:
        return cli.Outcome(cli.State.REVIEW, "https://pr")

    monkeypatch.setattr(cli, "run_live", fake_run_live)
    assert cli.main(["once"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "result: agent:review https://pr"
    )
