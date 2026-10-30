"""Regressions for task authority changing between discovery and work."""

from pathlib import Path

import httpx
import pytest

from agent_dispatcher import cli
from agent_dispatcher.github import GitHubReader
from agent_dispatcher.intake import Issue, accepts
from agent_dispatcher.roles import Team
from agent_dispatcher.settings import Settings
from agent_dispatcher.states import State


def test_closed_task_is_not_accepted_after_queue_discovery():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "event": "labeled",
                        "label": {"name": "agent:ready"},
                        "actor": {"login": "owner"},
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "number": 7,
                "title": "Old approved task",
                "body": "Add docs",
                "state": "closed",
                "labels": [{"name": "agent:ready"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        issue = GitHubReader(client).issue("owner/repo", 7)
    assert not accepts(issue, "owner")


@pytest.mark.parametrize(
    "change",
    [
        "unchanged",
        "proposal_annotation",
        "closed",
        "title",
        "body",
        "label",
        "actor",
        "private",
        "archived",
        "disabled",
        "owner",
        "missing_state",
        "missing_visibility",
        "missing_private",
        "full_name",
    ],
)
def test_authority_binds_fresh_scope_and_task(change: str):
    original = Issue(
        "owner/repo", 7, "Task", "Original", ("agent:ready",), "owner", is_open=True
    )
    repo: dict[str, object] = {
        "private": False,
        "archived": False,
        "disabled": False,
        "visibility": "public",
        "full_name": "owner/repo",
        "owner": {"login": "owner"},
    }
    issue: dict[str, object] = {
        "number": 7,
        "title": "Task",
        "body": "Original",
        "state": "open",
        "labels": [{"name": "agent:running"}],
    }
    if change == "proposal_annotation":
        issue["labels"] = [{"name": "agent:running"}, {"name": "agent:proposed"}]
    elif change == "closed":
        issue["state"] = "closed"
    elif change == "missing_state":
        del issue["state"]
    elif change in {"title", "body"}:
        issue[change] = "Changed"
    elif change == "label":
        issue["labels"] = [{"name": "agent:failed"}]
    elif change in {"missing_visibility", "missing_private"}:
        del repo[change.removeprefix("missing_")]

    changes: dict[str, dict[str, object]] = {
        "private": {"private": True},
        "archived": {"archived": True},
        "disabled": {"disabled": True},
        "full_name": {"full_name": "owner/different-repo"},
        "owner": {"owner": {"login": "someone"}},
    }
    repo.update(changes.get(change, {}))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "event": "labeled",
                        "label": {"name": "agent:ready"},
                        "actor": {"login": "someone" if change == "actor" else "owner"},
                    }
                ],
            )
        return httpx.Response(
            200, json=issue if "/issues/" in request.url.path else repo
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert GitHubReader(client).authorized_task(
            original, "owner", State.RUNNING
        ) is (change in {"unchanged", "proposal_annotation"})


def test_production_run_wires_authority_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):

    original = Issue(
        "owner/repo", 7, "Task", "Original", ("agent:ready",), "owner", is_open=True
    )
    calls: list[tuple[Issue, str, State]] = []

    def authorized(
        _reader: GitHubReader, issue: Issue, owner: str, expected: State
    ) -> bool:
        calls.append((issue, owner, expected))
        return False

    monkeypatch.setattr(GitHubReader, "authorized_task", authorized)
    settings = Settings(owner="owner", work_root=tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as client:
        run = cli.make_run(
            settings,
            original,
            run_id=1,
            team=Team("claude", "mimo", "codex"),
            client=client,
            root=tmp_path,
        )
        assert run.authorize is not None
        assert run.authorize(original, State.RUNNING) is False
    assert calls == [(original, "owner", State.RUNNING)]


def test_ambiguous_agent_labels_are_rejected_at_intake():
    issue = Issue(
        "owner/repo",
        7,
        "Task",
        "Original",
        ("agent:ready", "agent:failed"),
        "owner",
        is_open=True,
    )
    assert not accepts(issue, "owner")


@pytest.mark.parametrize("latest_actor", ["someone", None])
def test_latest_ready_actor_is_read_beyond_first_event_page(latest_actor: str | None):
    owner_label = {
        "event": "labeled",
        "label": {"name": "agent:ready"},
        "actor": {"login": "owner"},
    }
    attacker_label = {
        "event": "labeled",
        "label": {"name": "agent:ready"},
        "actor": {"login": latest_actor} if latest_actor is not None else None,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        events = (
            [owner_label, *[{"event": "commented"}] * 99]
            if page == "1"
            else [attacker_label]
        )
        return httpx.Response(200, json=events)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert GitHubReader(client).labelled_by("owner/repo", 7) == latest_actor


def test_owner_authorized_audit_proposal_annotation_does_not_block_intake():
    issue = Issue(
        "owner/repo",
        7,
        "Task",
        "Original",
        ("agent:ready", "agent:proposed"),
        "owner",
        is_open=True,
    )
    assert accepts(issue, "owner")
