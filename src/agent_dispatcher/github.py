"""Read-only GitHub access for polling. Writes come later, through the App."""

from typing import Any

import httpx

from agent_dispatcher.intake import Issue
from agent_dispatcher.states import State

API = "https://api.github.com"


class GitHubReader:
    """Lists ready issues and who labelled them."""

    def __init__(self, client: httpx.Client, token: str | None = None) -> None:
        """Use `client` for every call; a token only raises the rate limit."""
        self._client = client
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        response = self._client.get(
            f"{API}{path}", params=params, headers=self._headers, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def ready_issues(self, repo: str) -> list[Issue]:
        """Return open issues (not PRs) carrying the ready label."""
        rows = self._get(
            f"/repos/{repo}/issues",
            {"labels": State.READY.value, "state": "open", "per_page": 50},
        )
        return [
            Issue(
                repo=repo,
                number=row["number"],
                title=row["title"],
                body=row.get("body") or "",
                labels=tuple(label["name"] for label in row["labels"]),
                labelled_by=self.labelled_by(repo, row["number"]),
            )
            for row in rows
            if "pull_request" not in row
        ]

    def labelled_by(self, repo: str, number: int) -> str | None:
        """Return who most recently added the ready label, from the issue events."""
        events = self._get(f"/repos/{repo}/issues/{number}/events", {"per_page": 100})
        actors = [
            event["actor"]["login"]
            for event in events
            if event.get("event") == "labeled"
            and event.get("label", {}).get("name") == State.READY.value
            and event.get("actor")
        ]
        return actors[-1] if actors else None
