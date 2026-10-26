"""Git work for a run: the agent edits one clone, the controller publishes another.

The agent can write anything inside its clone, including `.git/hooks` and
`.git/config`. So the controller never pushes from it: it takes the diff, applies
it to a fresh clone, gates that clone and pushes from there, with hooks disabled.
"""

from collections.abc import Mapping
from pathlib import Path

from agent_dispatcher.gate import FileChange, parse_numstat
from agent_dispatcher.providers import Runner

_SYMLINK_MODE = "mode 120000"


class GitError(RuntimeError):
    """A git command failed."""


class Workspace:
    """Clones, diffs and pushes under one work root."""

    def __init__(self, root: Path, runner: Runner, env: Mapping[str, str]) -> None:
        """Use `root` for clones; git runs with `env` and hooks pointed at nothing."""
        self._root = root
        self._runner = runner
        self._env = env
        self._no_hooks = root / "no-hooks"
        self._no_hooks.mkdir(parents=True, exist_ok=True)

    def _git(self, cwd: Path, *args: str, stdin: str = "") -> str:
        argv = [
            "git",
            "-c",
            f"core.hooksPath={self._no_hooks}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            *args,
        ]
        code, stdout, stderr = self._runner(argv, cwd, stdin, self._env)
        if code != 0:
            message = f"git {args[0]} failed: {stderr.strip()[:500]}"
            raise GitError(message)
        return stdout

    def clone(self, url: str, name: str, branch: str | None = None) -> Path:
        """Shallow-clone `url` (at `branch`, if given) into a new directory."""
        destination = self._root / name
        chosen = ["--branch", branch] if branch else []
        self._git(self._root, "clone", "--depth", "1", "--no-tags", *chosen, url, name)
        return destination

    def default_branch(self, clone: Path) -> str:
        """Return the branch the clone checked out."""
        return self._git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def patch(self, clone: Path) -> str:
        """Return every change in the agent's clone as one binary-safe patch."""
        self._git(clone, "add", "--all")
        return self._git(
            clone, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"
        )

    def apply(self, clone: Path, patch: str) -> tuple[list[FileChange], list[str]]:
        """Apply the patch to a clean clone; return its changes and any symlinks."""
        self._git(clone, "apply", "--index", "--whitespace=nowarn", "-", stdin=patch)
        numstat = self._git(clone, "diff", "--cached", "--numstat", "--no-renames")
        summary = self._git(clone, "diff", "--cached", "--summary", "--no-renames")
        symlinks = [
            line.rsplit(" ", 1)[-1]
            for line in summary.splitlines()
            if _SYMLINK_MODE in line
        ]
        return parse_numstat(numstat), symlinks

    def push(self, clone: Path, branch: str, message: str) -> None:
        """Commit the applied patch on a new branch and push it."""
        self._git(clone, "checkout", "-b", branch)
        self._git(clone, "commit", "--no-verify", "--file", "-", stdin=message)
        self._git(clone, "push", "--no-verify", "origin", branch)

    def push_update(self, clone: Path, branch: str, message: str) -> None:
        """Commit the applied patch on top of an existing branch and push it."""
        self._git(clone, "commit", "--no-verify", "--file", "-", stdin=message)
        self._git(clone, "push", "--no-verify", "origin", f"HEAD:refs/heads/{branch}")
