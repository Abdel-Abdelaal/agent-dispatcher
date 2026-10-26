"""GitHub writes, through the `gh` CLI and only from the controller."""

import re
from collections.abc import Mapping
from pathlib import Path

from agent_dispatcher.providers import Runner
from agent_dispatcher.states import State, transition


class PublishError(RuntimeError):
    """A GitHub write failed."""


class GitHubWriter:
    """Moves task labels, comments and opens draft PRs."""

    def __init__(self, runner: Runner, env: Mapping[str, str], cwd: Path) -> None:
        """Run `gh` with `env`, which carries the controller's own login."""
        self._runner = runner
        self._env = env
        self._cwd = cwd

    def _gh(self, *args: str, stdin: str = "") -> str:
        code, stdout, stderr = self._runner(["gh", *args], self._cwd, stdin, self._env)
        if code != 0:
            message = f"gh {args[0]} failed: {stderr.strip()[:500]}"
            raise PublishError(message)
        return stdout.strip()

    def move(self, repo: str, number: int, current: State, target: State) -> State:
        """Swap the task's state label, if the state machine allows the move."""
        transition(current, target)
        self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            repo,
            "--remove-label",
            current.value,
            "--add-label",
            target.value,
        )
        return target

    def comment(self, repo: str, number: int, body: str) -> None:
        """Comment on the task's issue."""
        self._gh(
            "issue",
            "comment",
            str(number),
            "--repo",
            repo,
            "--body-file",
            "-",
            stdin=body,
        )

    def pr_comment(self, repo: str, number: int, body: str) -> None:
        """Comment on a pull request."""
        self._gh(
            "pr", "comment", str(number), "--repo", repo, "--body-file", "-", stdin=body
        )

    def failed_log(self, repo: str, job_id: str, lines: int = 120) -> str:
        """Return the error lines of a failed Actions job's log, or "" if unreadable."""
        try:
            log = self._gh(
                "run", "view", "--repo", repo, "--job", job_id, "--log-failed"
            )
        except PublishError:
            return ""
        return error_lines(log, lines)

    def draft_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        """Open a draft PR labelled `ai-generated` and return its URL."""
        return self._gh(
            "pr",
            "create",
            "--repo",
            repo,
            "--draft",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--label",
            "ai-generated",
            "--body-file",
            "-",
            stdin=body,
        )


# `gh run view --log-failed` prefixes every line with job, step and timestamp.
_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
# Runner and harden-runner chatter that fills the end of every failed log.
_NOISE = re.compile(
    r"agentservice|module=armour|RingBuffer|Cleaning up orphan|##\[(?:group|endgroup)\]"
)
# Lines that say what failed: annotations, tool diagnostics, test failures.
_SIGNAL = re.compile(
    r"##\[error\]|\berror\b|Error|FAILED|^E\s|^\s*--!?>|\b[A-Z]{1,4}\d{3,4}\b"
    r"|\d+ (?:errors?|failed)|would reformat|Found \d+ error|==>"
)


def error_lines(log: str, limit: int = 120) -> str:
    """Return the lines of a CI log that say what failed, without runner noise.

    The end of a failed log is runner cleanup, not the failure, so keeping the
    tail hid every real error from the repairing model.
    """
    cleaned = [_PREFIX.sub("", line) for line in log.splitlines()]
    useful = [line for line in cleaned if line.strip() and not _NOISE.search(line)]
    signal = [line for line in useful if _SIGNAL.search(line)]
    return "\n".join((signal or useful)[-limit:])
