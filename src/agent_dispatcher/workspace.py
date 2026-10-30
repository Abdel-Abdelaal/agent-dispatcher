"""Git work for a run: the agent edits one clone, the controller publishes another.

The agent can write anything inside its clone, including `.git/hooks` and
`.git/config`. So the controller never pushes from it: it takes the diff, applies
it to a fresh clone, gates that clone and pushes from there, with hooks disabled.
Patch extraction uses a controller-owned copy of the original Git metadata,
filters disabled and credentials removed; agent config, HEAD and index are ignored.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from agent_dispatcher.gate import FileChange, parse_numstat
from agent_dispatcher.providers import Runner, scrubbed_env

_SYMLINK_MODE = "mode 120000"


class GitError(RuntimeError):
    """A git command failed."""


class Workspace:
    """Clones, diffs and pushes under one work root."""

    def __init__(self, root: Path, runner: Runner, env: Mapping[str, str]) -> None:
        """Use `root` for clones; git runs with `env` and hooks pointed at nothing."""
        root = root.resolve()
        self._root = root
        self._runner = runner
        self._env = env
        self._no_hooks = root / "no-hooks"
        self._no_hooks.mkdir(parents=True, exist_ok=True)
        self._metadata_root = root / "controller-git"
        self._metadata_root.mkdir(mode=0o700, exist_ok=True)
        self._metadata: dict[Path, Path] = {}

    def _git(
        self,
        cwd: Path,
        *args: str,
        stdin: str = "",
        environment: Mapping[str, str] | None = None,
    ) -> str:
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
        env = self._env if environment is None else environment
        code, stdout, stderr = self._runner(argv, cwd, stdin, env)
        if code != 0:
            message = f"git {args[0]} failed: {stderr.strip()[:500]}"
            raise GitError(message)
        return stdout

    def clone(self, url: str, name: str, branch: str | None = None) -> Path:
        """Shallow-clone `url` (at `branch`, if given) into a new directory."""
        destination = self._root / name
        chosen = ["--branch", branch] if branch else []
        self._git(
            self._root,
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--no-checkout",
            *chosen,
            url,
            name,
        )
        # Highest-precedence attributes disable configured clean/smudge filters
        # before the first checkout. Agent-written attributes cannot override this.
        attributes = destination / ".git/info/attributes"
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text("* -filter\n", encoding="utf-8")
        self._git(destination, "read-tree", "--reset", "-u", "HEAD")
        metadata = self._metadata_root / name
        shutil.copytree(destination / ".git", metadata)
        self._metadata[destination.resolve()] = metadata.resolve()
        return destination

    def default_branch(self, clone: Path) -> str:
        """Return the branch the clone checked out."""
        return self._git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def patch(self, clone: Path) -> str:
        """Return every change in the agent's clone as one binary-safe patch."""
        prefix, env = self._trusted_context(clone)
        self._git(self._root, *prefix, "add", "--all", environment=env)
        return self._git(
            self._root,
            *prefix,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            environment=env,
        )

    def base(self, clone: Path) -> str:
        """Read the original base from controller-owned metadata, never agent HEAD."""
        prefix, env = self._trusted_context(clone)
        return self._git(
            self._root, *prefix, "rev-parse", "HEAD", environment=env
        ).strip()

    def _trusted_context(self, clone: Path) -> tuple[tuple[str, ...], dict[str, str]]:
        metadata = self._metadata.get(clone.resolve())
        if metadata is None:
            message = "No controller-owned Git metadata for this clone."
            raise GitError(message)
        env = scrubbed_env(self._env)
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            HOME=str(self._metadata_root / "empty-home"),
            XDG_CONFIG_HOME=str(self._metadata_root / "empty-home"),
        )
        prefix = ("--git-dir", str(metadata), "--work-tree", str(clone.resolve()))
        return prefix, env

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
