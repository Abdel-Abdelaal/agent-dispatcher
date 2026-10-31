"""Read-only continuation of a retained native call, checked before any model runs.

The frozen continuation-acceptance-v1 contract scopes this first step to read-only
calls: no write-stage or failed-task restart. A retained call is resumable only
when the controller bound it to the same task, base, candidate and policy that it
sees now, its receipts are complete, and exactly one native session exists. The
original profile is never written; continuation runs on a copy with a parent
reference, and the caller must check the task and candidate again before using
the answer for anything.
"""

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_dispatcher.native_evidence import CallBinding
from agent_dispatcher.opencode_worker import MiMoOpenCode
from agent_dispatcher.providers import Runner, RunResult, subprocess_runner
from agent_dispatcher.telemetry import sha256

_DB = Path("data/opencode/opencode.db")
_FILES = ("metadata.json", "result.json", "candidate.json", "config.sha256")
_WRITE_TOOLS = {"edit", "write", "apply_patch", "batch"}
REASONS = {
    "task_not_authorized",
    "path_escape",
    "unbound_legacy_trace",
    "write_stage",
    "corrupt_state",
    "missing_receipt",
    "changed_task",
    "changed_candidate",
    "incompatible_harness",
    "ambiguous_state",
    "pending_write",
    "continuation_active",
}


class RefusedError(ValueError):
    """The retained call can't be continued; `reason` is one of `REASONS`."""

    def __init__(self, reason: str) -> None:
        """Keep the fixed-vocabulary reason for callers and telemetry."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Source:
    """A retained call that passed every check, as of when it was checked."""

    profile: Path
    session_id: str
    binding: CallBinding
    reasoning: str
    workspace: str


@dataclass(frozen=True)
class Continuation:
    """The continued call's result, its own profile and whether it is still current."""

    result: RunResult
    profile: Path
    current: bool


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _contained(root: Path, profile: Path) -> None:
    """Accept only a real directory directly under `root`, with no links inside."""
    try:
        base = root.resolve(strict=True)
        real = profile.resolve(strict=True)
    except OSError:
        raise RefusedError("path_escape") from None
    if real.parent != base or _linked(profile) or not real.is_dir():
        raise RefusedError("path_escape")
    for part in (Path("data"), Path("data/opencode"), _DB, *map(Path, _FILES)):
        if _linked(real / part):
            raise RefusedError("path_escape")


def _json(path: Path) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RefusedError("missing_receipt") from None
    except (OSError, ValueError):
        raise RefusedError("corrupt_state") from None
    if not isinstance(value, dict):
        raise RefusedError("corrupt_state")
    return cast("dict[str, Any]", value)


def _binding(metadata: dict[str, Any]) -> CallBinding:
    raw = metadata.get("binding")
    if raw is None:
        raise RefusedError("unbound_legacy_trace")
    try:
        binding = CallBinding(**cast("dict[str, Any]", raw))
    except TypeError:
        raise RefusedError("corrupt_state") from None
    fields = asdict(binding)
    if type(fields["run_id"]) is not int or not all(
        isinstance(value, str) for key, value in fields.items() if key != "run_id"
    ):
        raise RefusedError("corrupt_state")
    return binding


def _compare(retained: CallBinding, current: CallBinding) -> None:
    if retained.task_sha256 != current.task_sha256:
        raise RefusedError("changed_task")
    if (retained.base_commit, retained.patch_sha256, retained.policy_sha256) != (
        current.base_commit,
        current.patch_sha256,
        current.policy_sha256,
    ):
        raise RefusedError("changed_candidate")


def _session(profile: Path, harness_version: str) -> str:
    """Return the only live native session; refuse ambiguity or pending writes."""
    try:
        uri = (profile / _DB).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            sessions = db.execute(
                "SELECT id, version FROM session WHERE time_archived IS NULL"
            ).fetchall()
            pending = db.execute(
                "SELECT json_extract(data,'$.tool') FROM part "
                "WHERE json_extract(data,'$.type')='tool' "
                "AND json_extract(data,'$.state.status') IN ('pending','running')"
            ).fetchall()
    except sqlite3.Error:
        raise RefusedError("corrupt_state") from None
    if len(sessions) != 1 or not isinstance(sessions[0][0], str):
        raise RefusedError("ambiguous_state")
    if sessions[0][1] != harness_version:
        raise RefusedError("incompatible_harness")
    if any(tool in _WRITE_TOOLS or not isinstance(tool, str) for (tool,) in pending):
        raise RefusedError("pending_write")
    return sessions[0][0]


def verify(  # noqa: PLR0913 - every input is a fact the controller must supply
    root: Path,
    profile: Path,
    *,
    current: CallBinding,
    authorized: bool,
    config_sha256: str,
    harness_version: str,
) -> Source:
    """Check a retained call against the controller's current view, before any model."""
    if not authorized:
        raise RefusedError("task_not_authorized")
    _contained(root, profile)
    metadata = _json(profile / "metadata.json")
    binding = _binding(metadata)
    if metadata.get("read_only") is not True:
        raise RefusedError("write_stage")
    _compare(binding, current)
    _json(profile / "result.json")
    receipt = _json(profile / "candidate.json")
    if receipt.get("status") != "captured" or receipt.get("retention_errors") != []:
        raise RefusedError("missing_receipt")
    if receipt.get("patch_sha256") != binding.patch_sha256:
        raise RefusedError("changed_candidate")
    try:
        retained_config = (profile / "config.sha256").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RefusedError("missing_receipt") from None
    if retained_config != config_sha256:
        raise RefusedError("incompatible_harness")
    reasoning = metadata.get("reasoning")
    workspace = metadata.get("workspace")
    if reasoning not in {"enabled", "disabled"} or not isinstance(workspace, str):
        raise RefusedError("corrupt_state")
    return Source(
        profile=profile,
        session_id=_session(profile, harness_version),
        binding=binding,
        reasoning=reasoning,
        workspace=workspace,
    )


@contextmanager
def exclusive(root: Path, source: Source) -> Generator[None]:
    """Allow one continuation of a source at a time; a stale lock stays refused."""
    lock = root / (source.profile.name + ".continuation.lock")
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        raise RefusedError("continuation_active") from None
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def fork(root: Path, source: Source, prompt: str) -> Path:
    """Copy the source's native state into a new profile that names its parent."""
    profile = root / f"{source.profile.name}-c{uuid.uuid4().hex[:12]}"
    target = profile / _DB
    target.parent.mkdir(parents=True)
    uri = (source.profile / _DB).as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True, timeout=1)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)
    metadata = (source.profile / "metadata.json").read_bytes()
    (profile / "metadata.json").write_text(
        json.dumps(
            {
                "workspace": source.workspace,
                "started_at": datetime.now(UTC).isoformat(),
                "prompt_sha256": sha256(prompt),
                "read_only": True,
                "reasoning": source.reasoning,
                "binding": asdict(source.binding),
                "parent": {
                    "profile": source.profile.name,
                    "session_id": source.session_id,
                    "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
                    "database_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    return profile


def resume(  # noqa: PLR0913 - the controller's checks and the call travel together
    worker: MiMoOpenCode,
    prompt: str,
    worktree: Path,
    profile: Path,
    *,
    current: Callable[[], tuple[CallBinding, bool]],
    harness_version: str,
    steps: int,
    runner: Runner = subprocess_runner,
    environ: Mapping[str, str] | None = None,
) -> Continuation:
    """Verify, fork and continue read-only, then check the task and candidate again.

    `current` returns the controller's binding for the task and candidate as they
    are now, and whether the task is still open and authorized. A continuation
    that is no longer current keeps its result and usage for the record, but the
    caller must not publish or approve anything from it.
    """
    root = worker.checkpoint_root
    if root is None:
        message = "Continuation requires retained checkpoint state."
        raise ValueError(message)
    binding, authorized = current()
    source = verify(
        root,
        profile,
        current=binding,
        authorized=authorized,
        config_sha256=sha256(worker.config(read_only=True, steps=steps)),
        harness_version=harness_version,
    )
    with exclusive(root, source):
        child = fork(root, source, prompt)
        result = worker.resume(
            prompt,
            worktree,
            profile=child,
            session=source.session_id,
            steps=steps,
            runner=runner,
            environ=environ,
        )
    after, still_authorized = current()
    try:
        _compare(source.binding, after)
        fresh = still_authorized
    except RefusedError:
        fresh = False
    return Continuation(result=result, profile=child, current=fresh)
