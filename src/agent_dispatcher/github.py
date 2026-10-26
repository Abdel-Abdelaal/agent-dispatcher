"""Read-only GitHub access for polling. Writes come later, through the App."""

import re
from dataclasses import dataclass
from typing import Any

import httpx

from agent_dispatcher.intake import Issue
from agent_dispatcher.states import State

API = "https://api.github.com"
_AGENT_BRANCH = re.compile(r"^agent/(?P<issue>\d+)-run-\d+$")
# Advisory checks never block a merge, so they never trigger a repair either.
_ADVISORY = ("review / ai",)


@dataclass(frozen=True)
class AgentPr:
    """An open PR on a branch agent-dispatcher pushed."""

    number: int
    branch: str
    sha: str
    issue: int


@dataclass(frozen=True)
class FailedCheck:
    """A finished check that failed, and the Actions job that ran it."""

    name: str
    job_id: str


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

    def issue(self, repo: str, number: int) -> Issue:
        """Return one issue as it is now, for a task queued in an earlier cycle."""
        response = self._client.get(
            f"{API}/repos/{repo}/issues/{number}", headers=self._headers, timeout=30
        )
        response.raise_for_status()
        row = response.json()
        return Issue(
            repo=repo,
            number=number,
            title=row["title"],
            body=row.get("body") or "",
            labels=tuple(label["name"] for label in row["labels"]),
            labelled_by=self.labelled_by(repo, number),
        )

    def agent_prs(self, repo: str) -> list[AgentPr]:
        """Return open PRs whose branch agent-dispatcher created in this repo."""
        rows = self._get(f"/repos/{repo}/pulls", {"state": "open", "per_page": 50})
        prs: list[AgentPr] = []
        for row in rows:
            head = row["head"]
            match = _AGENT_BRANCH.match(head["ref"])
            head_repo: dict[str, Any] = head.get("repo") or {}
            same_repo = head_repo.get("full_name") == repo
            if match and same_repo:
                issue = int(match["issue"])
                prs.append(AgentPr(row["number"], head["ref"], head["sha"], issue))
        return prs

    def failed_checks(self, repo: str, sha: str) -> list[FailedCheck] | None:
        """Return the failed blocking checks, or None while any check still runs."""
        response = self._client.get(
            f"{API}/repos/{repo}/commits/{sha}/check-runs",
            params={"per_page": 100},
            headers=self._headers,
            timeout=30,
        )
        response.raise_for_status()
        runs: list[dict[str, Any]] = response.json()["check_runs"]
        if any(run["status"] != "completed" for run in runs):
            return None
        return [
            FailedCheck(run["name"], str(run.get("details_url", "")).rsplit("/", 1)[-1])
            for run in runs
            if run["conclusion"] in {"failure", "timed_out"}
            and not run["name"].startswith(_ADVISORY)
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
