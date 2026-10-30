"""Regressions for task authority changing between discovery and work."""

import httpx
import pytest

from agent_dispatcher.github import GitHubReader
from agent_dispatcher.intake import Issue, accepts
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
    ],
)
def test_authority_binds_fresh_scope_and_task(change: str):
    original = Issue("owner/repo", 7, "Task", "Original", ("agent:ready",), "owner")
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
    if change == "closed":
        issue["state"] = "closed"
    elif change == "missing_state":
        del issue["state"]
    elif change in {"title", "body"}:
        issue[change] = "Changed"
    elif change == "label":
        issue["labels"] = [{"name": "agent:failed"}]
    elif change in {"private", "archived", "disabled"}:
        repo[change] = True
    elif change == "owner":
        repo["owner"] = {"login": "someone"}

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
        ) is (change == "unchanged")
