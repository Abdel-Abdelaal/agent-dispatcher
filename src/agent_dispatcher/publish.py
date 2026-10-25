"""GitHub writes, through the `gh` CLI and only from the controller."""

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
