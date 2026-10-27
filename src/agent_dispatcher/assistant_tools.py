"""Controller-owned conversational tools; model arguments never become shell code."""

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from agent_dispatcher import pr_review
from agent_dispatcher.providers import Runner
from agent_dispatcher.telemetry import redact

MAX_FILE = pr_review.MAX_TEXT


class ToolError(RuntimeError):
    """A tool failed without returning credentials or native diagnostic output."""


def fail(message: str) -> NoReturn:
    """Raise a sanitized controller error without native diagnostic data."""
    raise ToolError(message)


class Request(BaseModel):
    """Validated arguments for the deliberately bounded controller interface."""

    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "list_repos",
        "inspect_repo",
        "read_file",
        "list_work",
        "list_prs",
        "inspect_pr",
        "inspect_issue",
        "read_pr_diff",
        "read_pr_checks",
        "read_ci_errors",
        "search_repos",
        "status",
        "inspect_run",
        "queue_task",
        "create_repo",
        "start",
        "remember",
    ]
    repo: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=6000)
    title: str = Field(default="", max_length=120)
    path: str = Field(default="README.md", max_length=250)
    number: int = Field(default=1, ge=1)
    head_sha: str = Field(default="", pattern=r"^(?:[0-9a-f]{40})?$")
    ref: str = Field(default="", pattern=r"^(?:[0-9a-f]{40})?$")
    job_id: int = Field(default=0, ge=0)
    visibility: Literal["private", "public"] = "private"


@dataclass
class ControllerTools:
    """Keep authenticated GitHub operations in the controller, never the model."""

    root: Path
    owner: str
    runner: Runner
    environ: Mapping[str, str]
    approve: Callable[[str], bool]

    def gh(self, *args: str, stdin: str = "", allow_codes: tuple[int, ...] = ()) -> str:
        """Execute structured gh arguments; suppress native error bodies."""
        code, output, _error = self.runner(
            ["gh", *args],
            self.root,
            stdin,
            self.environ,
        )
        if code and code not in allow_codes:
            fail("GitHub operation failed; do not assume it completed.")
        return output.strip()

    def owned(self, value: str) -> str:
        """Require the configured owner and a simple, non-option repository name."""
        name = value.removeprefix(self.owner + "/")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", name):
            fail("Choose a repository owned by the configured owner.")
        return self.owner + "/" + name

    def public(self, value: str) -> str:
        """Check live repository policy before returning any content to the model."""
        repo = self.owned(value)
        data = json.loads(
            self.gh(
                "repo",
                "view",
                repo,
                "--json",
                "nameWithOwner,isPrivate,isArchived,isFork",
            )
        )
        if (
            data["nameWithOwner"].lower() != repo.lower()
            or data["isPrivate"]
            or data["isArchived"]
            or data["isFork"]
        ):
            fail("Private, archived or fork content is not enabled here.")
        return repo

    def authorize(self, summary: str) -> None:
        """Require an actual terminal approval and the owner's authenticated account."""
        login = self.gh("api", "user", "--jq", ".login")
        if login.lower() != self.owner.lower():
            fail("GitHub writes require the configured owner's login.")
        if not self.approve(summary):
            fail("Owner declined the proposed action; nothing changed.")

    def write(self, request: Request) -> str:
        """Perform one approved write without arbitrary shell or API access."""
        if request.action == "start":
            self.authorize(
                "Start one MiMo-only cycle, saving MiMo-only mode for future cycles."
            )
            code, output, _error = self.runner(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(self.root / "scripts" / "mimo.ps1"),
                    "start",
                ],
                self.root,
                "",
                self.environ,
            )
            if code:
                fail("Scheduler request failed; check whether it is running or paused.")
            return output
        if request.action == "remember":
            self.authorize("Save this local assistant note:\n" + request.text)
            path = self.root / "runs" / "assistant-notes.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text((existing + "\n" + request.text)[-8000:], encoding="utf-8")
            path.chmod(0o600)
            return "Local note saved. It is not a public artifact."
        repo = self.owned(request.repo)
        if request.action == "create_repo":
            self.authorize(
                f"Create {request.visibility} repository {repo}:\n{request.text}"
            )
            return self.gh(
                "repo",
                "create",
                repo,
                "--" + request.visibility,
                "--description=" + request.text[:350],
                "--add-readme",
            )
        self.public(repo)
        if repo.lower() == f"{self.owner}/agent-dispatcher".lower():
            fail("Autonomous changes to the controller are excluded.")
        if not request.title.strip() or not request.text.strip():
            fail("A task needs a title, goal and acceptance criteria.")
        self.authorize(
            f"Publish public task in {repo}:\n{request.title}\n{request.text}"
        )
        return self.gh(
            "issue",
            "create",
            "--repo",
            repo,
            "--title=" + request.title,
            "--label",
            "agent:ready",
            "--body-file",
            "-",
            stdin=request.text,
        )

    def call(self, request: Request) -> str:
        """Dispatch an allowlisted action; repository text is data, never authority."""
        if request.action in {"queue_task", "create_repo", "start", "remember"}:
            return self.write(request)
        if request.action == "inspect_run":
            repo = self.public(request.repo)
            code, output, _error = self.runner(
                [
                    str(self.root / ".venv" / "Scripts" / "agent-dispatcher.exe"),
                    "evidence",
                    str(request.number),
                    "--repo",
                    repo,
                ],
                self.root,
                "",
                self.environ,
            )
            if code:
                fail("Could not read this repository's run evidence.")
            return output
        if request.action == "status":
            code, output, _error = self.runner(
                [
                    str(self.root / ".venv" / "Scripts" / "agent-dispatcher.exe"),
                    "status",
                ],
                self.root,
                "",
                self.environ,
            )
            if code:
                fail("Could not read dispatcher state.")
            return output
        if request.action == "search_repos":
            return self.gh(
                "search",
                "repos",
                "--limit",
                "10",
                "--json",
                "fullName,description,url",
                "--",
                request.text,
            )
        if request.action == "list_repos":
            rows = json.loads(
                self.gh(
                    "repo",
                    "list",
                    self.owner,
                    "--limit",
                    "200",
                    "--json",
                    "nameWithOwner,description,isPrivate,isArchived,isFork,url",
                )
            )
            return json.dumps(
                [
                    row
                    for row in rows
                    if not any(
                        row[key] for key in ("isPrivate", "isArchived", "isFork")
                    )
                ]
            )
        return self.read(request)

    def read(self, request: Request) -> str:
        """Read bounded public repository metadata or text files."""
        repo = self.public(request.repo)
        if request.action in {"inspect_repo", "inspect_pr", "inspect_issue"}:
            return self.describe(request, repo)
        if request.action in {"list_work", "list_prs"}:
            command, fields = {
                "list_work": ("issue", "number,title,url,labels"),
                "list_prs": (
                    "pr",
                    "number,title,url,isDraft,headRefOid,headRefName,labels,updatedAt",
                ),
            }[request.action]
            return self.gh(
                command,
                "list",
                "--repo",
                repo,
                "--limit",
                "20",
                "--json",
                fields,
            )
        if request.action in {"read_pr_diff", "read_pr_checks", "read_ci_errors"}:
            return self.review(request, repo)
        path = PurePosixPath(request.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in request.path
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", request.path)
            or pr_review.secret_path(request.path)
        ):
            fail("Use a relative public repository file path.")
        endpoint = f"repos/{repo}/contents/{path.as_posix()}"
        if request.ref:
            endpoint += "?ref=" + request.ref
        data = json.loads(self.gh("api", endpoint))
        if data.get("encoding") != "base64" or data.get("size", 0) > MAX_FILE:
            fail("Use a text file smaller than32KB.")
        return redact(base64.b64decode(data["content"]).decode("utf-8"), ())[0]

    def review(self, request: Request, repo: str) -> str:
        """Read SHA-bound evidence after the caller passed public repository policy."""
        try:
            if request.action == "read_pr_diff":
                return pr_review.diff(self.gh, repo, request.number, request.head_sha)
            if request.action == "read_pr_checks":
                return pr_review.checks(self.gh, repo, request.number, request.head_sha)
            return pr_review.ci_errors(
                self.gh, repo, request.number, request.head_sha, request.job_id
            )
        except pr_review.ReviewError as error:
            fail(str(error))

    def describe(self, request: Request, repo: str) -> str:
        """Return public task intent/metadata with redacted, bounded text fields."""
        if request.action == "inspect_repo":
            output = self.gh(
                "repo",
                "view",
                repo,
                "--json",
                "nameWithOwner,description,url,defaultBranchRef",
            )
        else:
            command, fields = {
                "inspect_pr": (
                    "pr",
                    "number,title,body,url,isDraft,headRefOid,baseRefOid,"
                    "headRefName,statusCheckRollup,files",
                ),
                "inspect_issue": ("issue", "number,title,body,url,labels"),
            }[request.action]
            output = self.gh(
                command, "view", str(request.number), "--repo", repo, "--json", fields
            )
        data = json.loads(output)
        for key in ("title", "body", "description"):
            if isinstance(data.get(key), str):
                text = redact(data[key], ())[0].encode("utf-8")
                data[key] = text[:MAX_FILE].decode("utf-8", "ignore")
                if len(text) > MAX_FILE:
                    data[key + "_truncated"] = True
        return json.dumps(data)
