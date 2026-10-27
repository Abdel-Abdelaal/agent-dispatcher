"""Read immutable public candidate diffs and head-matched CI diagnostic evidence."""

import json
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, NoReturn

from agent_dispatcher.publish import error_lines
from agent_dispatcher.telemetry import redact

GitHub = Callable[..., str]
MAX_TEXT = 32000
MAX_FILES = 30
MAX_LINES = 1000
_SHA = re.compile(r"[0-9a-f]{40}")


class ReviewError(RuntimeError):
    """A fixed controller diagnostic, never an arbitrary GitHub error body."""


def secret_path(path: str) -> bool:
    """Apply the same secret-file policy to contents and both sides of a diff."""
    return any(
        part.lower().startswith(".env")
        for part in PurePosixPath(path.replace("\\", "/").strip('"')).parts
    )


def refuse(message: str) -> NoReturn:
    """Stop with a controller-owned message, excluding native error payloads."""
    raise ReviewError(message)


def snapshot(gh: GitHub, repo: str, number: int, expected: str) -> dict[str, Any]:
    """Require a caller-observed SHA so evidence cannot silently move to new code."""
    if not _SHA.fullmatch(expected):
        refuse("Inspect the PR and supply its full head commit SHA.")
    data: dict[str, Any] = json.loads(
        gh(
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "headRefOid,baseRefOid,changedFiles,additions,deletions",
        )
    )
    if data.get("headRefOid") != expected:
        refuse("PR head changed; inspect it again before reviewing.")
    if not isinstance(data.get("baseRefOid"), str) or not _SHA.fullmatch(
        data["baseRefOid"]
    ):
        refuse("PR base commit was not available.")
    return data


def diff(gh: GitHub, repo: str, number: int, head: str) -> str:
    """Fetch an immutable base...head comparison, refusing oversized evidence."""
    before = snapshot(gh, repo, number, head)
    if (
        before["changedFiles"] > MAX_FILES
        or before["additions"] + before["deletions"] > MAX_LINES
    ):
        refuse("Diff too large; inspect individual files at the commit SHA.")
    body = gh(
        "api",
        "--header",
        "Accept: application/vnd.github.diff",
        f"repos/{repo}/compare/{before['baseRefOid']}...{head}",
    )
    if len(body.encode("utf-8")) > MAX_TEXT:
        refuse("Diff too large; inspect individual files at the commit SHA.")
    headers = (
        "diff --git ",
        "--- ",
        "+++ ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
    )
    if any(
        secret_path(token)
        for line in body.splitlines()
        if line.startswith(headers)
        for token in line.split()
    ):
        refuse(
            "Diff includes secret configuration paths; inspect safe files individually."
        )
    after = snapshot(gh, repo, number, head)
    if after["baseRefOid"] != before["baseRefOid"]:
        refuse("PR base changed; inspect it again before reviewing.")
    body, matches = redact(body, ())
    return json.dumps(
        {
            "head_sha": head,
            "base_sha": before["baseRefOid"],
            "redacted": bool(matches),
            "diff": body,
        }
    )


def ci_errors(gh: GitHub, repo: str, number: int, head: str, job_id: int) -> str:
    """Read error lines only after verifying the job belongs to the observed head."""
    snapshot(gh, repo, number, head)
    if job_id < 1:
        refuse("Supply the Actions job ID from this PR's CI check link.")
    job: dict[str, Any] = json.loads(gh("api", f"repos/{repo}/actions/jobs/{job_id}"))
    if job.get("head_sha") != head:
        refuse("CI job does not belong to the observed PR head.")
    if job.get("status") != "completed":
        refuse("CI job is still running; diagnostic logs are not final.")
    log = gh("run", "view", "--repo", repo, "--job", str(job_id), "--log-failed")
    snapshot(gh, repo, number, head)
    text, matches = redact(error_lines(log), ())
    encoded = text.encode("utf-8")
    return json.dumps(
        {
            "head_sha": head,
            "job_id": job_id,
            "redacted": bool(matches),
            "truncated": len(encoded) > MAX_TEXT,
            "errors": encoded[:MAX_TEXT].decode("utf-8", "ignore"),
        }
    )


def checks(gh: GitHub, repo: str, number: int, head: str) -> str:
    """Read required check classifications, retaining failing/pending outcomes."""
    snapshot(gh, repo, number, head)
    output = gh(
        "pr",
        "checks",
        str(number),
        "--repo",
        repo,
        "--required",
        "--json",
        "name,state,link",
        allow_codes=(1, 8),
    )
    try:
        rows = json.loads(output)
    except ValueError:
        refuse("Required-check classification unavailable; do not infer approval.")
    if not isinstance(rows, list):
        refuse("Required-check classification unavailable; do not infer approval.")
    snapshot(gh, repo, number, head)
    return json.dumps({"head_sha": head, "required_checks": rows})
