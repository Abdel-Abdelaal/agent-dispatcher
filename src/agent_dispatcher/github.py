"""Read-only GitHub access for polling. Writes come later, through the App."""

import re
from dataclasses import dataclass
from typing import Any, cast

import httpx

from agent_dispatcher.intake import Issue
from agent_dispatcher.states import State, matches_state

API = "https://api.github.com"
_AGENT_BRANCH = re.compile(r"^agent/(?P<issue>\d+)-run-\d+$")
# Advisory checks never block a merge, so they never trigger a repair either.
_ADVISORY = ("review / ai",)
# An Actions check run links to its job; any other link has no log to read.
_JOB_URL = "https://github.com/{repo}/actions/runs/[0-9]+/job/(?P<job>[0-9]+)"
_EVENTS_PAGE_SIZE = 100


@dataclass(frozen=True)
class AgentPr:
    """An open PR on a branch agent-dispatcher pushed."""

    number: int
    branch: str
    sha: str
    issue: int
    base: str = "main"


@dataclass(frozen=True)
class FailedCheck:
    """A finished check that failed, and the Actions job that ran it ("" if none)."""

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
                is_open=row.get("state") == "open",
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
            is_open=row.get("state") == "open",
        )

    def authorized_task(self, original: Issue, owner: str, expected: State) -> bool:
        """Recheck public scope, raw task content and the owner's current authority."""
        response = self._client.get(
            f"{API}/repos/{original.repo}", headers=self._headers, timeout=30
        )
        response.raise_for_status()
        repo = response.json()
        if not (
            repo.get("private") is False
            and repo.get("archived") is False
            and repo.get("disabled") is False
            and repo.get("visibility") == "public"
            and str(repo.get("full_name", "")).lower() == original.repo.lower()
            and str(repo.get("owner", {}).get("login", "")).lower() == owner.lower()
        ):
            return False
        current = self.issue(original.repo, original.number)
        return (
            original.is_open
            and current.is_open
            and current.title == original.title
            and current.body == original.body
            and current.labelled_by == owner
            and matches_state(current.labels, expected)
        )

    def owned_public_repos(self, owner: str) -> list[str]:
        """Return the owner's public, non-fork, non-archived repositories.

        Private repositories are never discovered: their names must not appear in
        public evidence such as telemetry.
        """
        rows = self._get(
            f"/users/{owner}/repos",
            {"type": "owner", "per_page": 100, "sort": "full_name"},
        )
        return [
            row["full_name"]
            for row in rows
            if not row.get("private")
            and not row.get("fork")
            and not row.get("archived")
        ]

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
                base_info: dict[str, Any] = row.get("base") or {}
                base = str(base_info.get("ref", "main"))
                prs.append(
                    AgentPr(row["number"], head["ref"], head["sha"], issue, base)
                )
        return prs

    def required_checks(self, repo: str, branch: str) -> set[str]:
        """Return the checks the branch's rulesets require; empty if unreadable."""
        response = self._client.get(
            f"{API}/repos/{repo}/rules/branches/{branch}",
            headers=self._headers,
            timeout=30,
        )
        if not response.is_success:
            return set()
        required: set[str] = set()
        for rule in response.json():
            if rule.get("type") == "required_status_checks":
                checks = rule.get("parameters", {}).get("required_status_checks", [])
                required |= {check["context"] for check in checks}
        return required

    def failed_checks(
        self, repo: str, sha: str, required: set[str] | None = None
    ) -> list[FailedCheck] | None:
        """Return failed blocking checks, or None while any check still runs.

        With `required`, only those checks count: a failing advisory or optional
        check is never worth a repair, and an unreadable ruleset means no repair.
        """
        response = self._client.get(
            f"{API}/repos/{repo}/commits/{sha}/check-runs",
            params={"per_page": 100},
            headers=self._headers,
            timeout=30,
        )
        response.raise_for_status()
        runs: list[dict[str, Any]] = [
            run
            for run in response.json()["check_runs"]
            if not run["name"].startswith(_ADVISORY)
            and (required is None or run["name"] in required)
        ]
        # Only the checks that count are waited for: a slow advisory review must
        # not hold up the repair of a required check that has already failed.
        if any(run["status"] != "completed" for run in runs):
            return None
        job = re.compile(_JOB_URL.format(repo=re.escape(repo)))
        failed: list[FailedCheck] = []
        for run in runs:
            if run["conclusion"] not in {"failure", "timed_out"}:
                continue
            match = job.fullmatch(str(run.get("details_url") or ""))
            failed.append(FailedCheck(run["name"], match["job"] if match else ""))
        return failed

    def labelled_by(self, repo: str, number: int) -> str | None:
        """Return who most recently added the ready label, from the issue events."""
        page = 1
        actor: str | None = None
        while True:
            events = self._get(
                f"/repos/{repo}/issues/{number}/events",
                {"per_page": _EVENTS_PAGE_SIZE, "page": page},
            )
            for event in events:
                if (
                    event.get("event") == "labeled"
                    and event.get("label", {}).get("name") == State.READY.value
                ):
                    current_actor = event.get("actor")
                    login = (
                        cast("dict[str, object]", current_actor).get("login")
                        if isinstance(current_actor, dict)
                        else None
                    )
                    actor = login if isinstance(login, str) else None
            if len(events) < _EVENTS_PAGE_SIZE:
                return actor
            page += 1
