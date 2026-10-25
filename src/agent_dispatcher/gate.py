"""The diff gate: the controller, not the agent, decides what may become a PR."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# Edits here could change CI, review rules, or what the next agent run executes.
# `.claude/` and `.mcp.json` matter most: a headless run loads a repo's hooks and
# MCP servers, so one merged edit would run code on every later run.
PROTECTED_PATTERNS: tuple[str, ...] = (
    ".github/*",
    ".claude/*",
    ".codex/*",
    ".mcp.json",
    "codeowners",
    "agents.md",
    "claude.md",
    "opencode.json",
    ".pre-commit-config.yaml",
    ".env*",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "gradle/wrapper/*",
)
_NUMSTAT = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")


@dataclass(frozen=True)
class FileChange:
    """One file in a run's diff; binary files count as None lines."""

    path: str
    added: int | None
    deleted: int | None


@dataclass(frozen=True)
class Limits:
    """Caps on a single run's diff."""

    max_files: int
    max_lines: int
    allow_test_deletions: bool = False


class MalformedDiffError(ValueError):
    """The diff summary could not be parsed, so the gate fails closed."""


def parse_numstat(text: str) -> list[FileChange]:
    """Parse `git diff --numstat` output, failing closed on anything unexpected."""
    changes: list[FileChange] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _NUMSTAT.match(line)
        if match is None:
            message = f"unexpected numstat line: {line!r}"
            raise MalformedDiffError(message)
        added, deleted, path = match.groups()
        changes.append(
            FileChange(
                path=path,
                added=None if added == "-" else int(added),
                deleted=None if deleted == "-" else int(deleted),
            )
        )
    return changes


def _normalise(path: str) -> str:
    """Lower-case and use forward slashes, so case or separators dodge nothing."""
    return path.replace("\\", "/").lower()


def is_protected(path: str) -> bool:
    """Return True if the path, at any depth, matches a protected pattern."""
    parts = PurePosixPath(_normalise(path)).parts
    suffixes = ["/".join(parts[index:]) for index in range(len(parts))]
    return any(
        fnmatchcase(suffix, pattern)
        for suffix in suffixes
        for pattern in PROTECTED_PATTERNS
    )


def _is_test(path: str) -> bool:
    parts = PurePosixPath(_normalise(path)).parts
    return "tests" in parts or parts[-1].startswith("test_")


def refusals(changes: Iterable[FileChange], limits: Limits) -> list[str]:
    """Return every reason to refuse this diff; an empty list means it passes."""
    changes = list(changes)
    reasons: list[str] = []
    for change in changes:
        if ".." in PurePosixPath(_normalise(change.path)).parts:
            reasons.append(f"path escapes the repository: {change.path}")
        if is_protected(change.path):
            reasons.append(f"protected path: {change.path}")
        if change.added is None:
            reasons.append(f"binary file: {change.path}")
        if (
            not limits.allow_test_deletions
            and _is_test(change.path)
            and change.added == 0
            and (change.deleted or 0) > 0
        ):
            reasons.append(f"test lines deleted without additions: {change.path}")
    if len(changes) > limits.max_files:
        reasons.append(f"{len(changes)} files changed, limit {limits.max_files}")
    lines = sum((change.added or 0) + (change.deleted or 0) for change in changes)
    if lines > limits.max_lines:
        reasons.append(f"{lines} lines changed, limit {limits.max_lines}")
    return reasons
