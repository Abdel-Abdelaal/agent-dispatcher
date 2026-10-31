"""Read-only continuation follows the frozen continuation-acceptance-v1 contract."""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_dispatcher import continuation, native_evidence
from agent_dispatcher.continuation import RefusedError
from agent_dispatcher.native_evidence import CallBinding
from agent_dispatcher.opencode_worker import MiMoOpenCode
from agent_dispatcher.telemetry import sha256

VERSION = "1.18.35"
STEPS = 7
BINDING = CallBinding(
    run_id=1,
    stage="review",
    task_sha256="task",
    base_commit="base",
    patch_sha256="patch",
    policy_sha256="policy",
)


class Recorder:
    """Fake native runner: records the call and never contacts a provider."""

    def __init__(self, root: Path, reply: str = "answer") -> None:
        self.root = root
        self.reply = reply
        self.calls: list[tuple[list[str], Path, Mapping[str, str]]] = []
        self.locked: list[bool] = []

    def __call__(
        self, argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        del stdin
        self.calls.append((argv, cwd, env))
        self.locked.append(any(path.suffix == ".lock" for path in self.root.iterdir()))
        text = json.dumps({"type": "text", "part": {"text": self.reply}})
        return 0, text, ""


def worker(root: Path) -> MiMoOpenCode:
    return MiMoOpenCode(
        SecretStr("test-only"),
        checkpoint_root=root,
        candidate_snapshot=lambda: "patch",
    )


def database(
    profile: Path, *, sessions: int = 1, parts: Sequence[Mapping[str, object]] = ()
) -> None:
    path = profile / "data/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE session(id TEXT, version TEXT, time_archived INTEGER)")
        db.execute("CREATE TABLE part(data TEXT)")
        db.executemany(
            "INSERT INTO session VALUES(?,?,NULL)",
            [(f"ses_{n}", VERSION) for n in range(sessions)],
        )
        db.executemany("INSERT INTO part VALUES(?)", [(json.dumps(p),) for p in parts])
        db.commit()


def retained(  # noqa: PLR0913 - each fixture fact can be broken by one test
    root: Path,
    *,
    binding: CallBinding | None = BINDING,
    read_only: bool = True,
    sessions: int = 1,
    parts: Sequence[Mapping[str, object]] = (),
    receipt: str | None = "patch",
) -> Path:
    profile = native_evidence.create(
        root,
        root.parent / "run-1-agent",
        "private prompt",
        read_only=read_only,
        reasoning="enabled",
        binding=binding,
    )
    database(profile, sessions=sessions, parts=parts)
    (profile / "config.sha256").write_text(
        sha256(worker(root).config(read_only=True, steps=STEPS)), encoding="utf-8"
    )
    native_evidence.finish(profile, 124, "private events")
    native_evidence.finish_candidate(profile, receipt)
    return profile


def snapshot(profile: Path) -> dict[str, str]:
    return {
        str(path.relative_to(profile)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(profile.rglob("*"))
        if path.is_file()
    }


def check(root: Path, profile: Path, **changes: object) -> continuation.Source:
    facts: dict[str, object] = {
        "current": BINDING,
        "authorized": True,
        "config_sha256": sha256(worker(root).config(read_only=True, steps=STEPS)),
        "harness_version": VERSION,
    }
    facts.update(changes)
    return continuation.verify(root, profile, **facts)  # type: ignore[arg-type]


def refused(reason: str, root: Path, profile: Path, **changes: object) -> None:
    before = snapshot(profile)
    with pytest.raises(RefusedError) as caught:
        check(root, profile, **changes)
    assert caught.value.reason == reason
    assert reason in continuation.REASONS
    assert snapshot(profile) == before


def test_valid_read_only_fork_preserves_the_original_and_names_its_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    before = snapshot(profile)
    runner = Recorder(root)
    environ = {"PATH": "x", "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret"}

    done = continuation.resume(
        worker(root),
        "continue the review",
        tmp_path,
        profile,
        current=lambda: (BINDING, True),
        harness_version=VERSION,
        steps=STEPS,
        runner=runner,
        environ=environ,
    )

    assert snapshot(profile) == before
    assert done.current is True
    assert done.result.text == "answer"
    ((argv, cwd, env),) = runner.calls
    assert cwd == tmp_path
    assert argv[argv.index("--session") + 1] == "ses_0"
    assert "--fork" in argv
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"] == "deny"
    assert config["permission"]["*"] == "deny"
    assert config["mcp"] == {}
    assert config["plugin"] == []
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["XDG_DATA_HOME"] == str(done.profile / "data")
    metadata = json.loads((done.profile / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["read_only"] is True
    assert metadata["parent"]["profile"] == profile.name
    assert metadata["parent"]["session_id"] == "ses_0"
    assert metadata["parent"]["metadata_sha256"] == before["metadata.json"]
    assert "private prompt" not in json.dumps(metadata)
    for name in ("events.jsonl", "result.json", "candidate.json", "config.sha256"):
        assert (done.profile / name).is_file()
    with closing(sqlite3.connect(done.profile / "data/opencode/opencode.db")) as db:
        assert db.execute("SELECT id FROM session").fetchall() == [("ses_0",)]


def test_continuation_profiles_appear_in_run_reports_beside_their_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    done = continuation.resume(
        worker(root),
        "continue",
        tmp_path,
        profile,
        current=lambda: (BINDING, True),
        harness_version=VERSION,
        steps=STEPS,
        runner=Recorder(root),
    )
    report = native_evidence.report(tmp_path, 1)
    assert done.profile.name.startswith(profile.name + "-c")
    assert len(report["native_evidence"]) == 2


def test_closed_or_changed_task_is_refused_before_any_read(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    refused("task_not_authorized", root, profile, authorized=False)
    refused(
        "changed_task", root, profile, current=replace(BINDING, task_sha256="edited")
    )
    # Authorization is checked first, so even a missing profile is not read.
    with pytest.raises(RefusedError, match="task_not_authorized"):
        check(root, root / "missing", authorized=False)


@pytest.mark.parametrize(
    "change",
    [
        {"base_commit": "other"},
        {"patch_sha256": "other"},
        {"policy_sha256": "other"},
    ],
)
def test_changed_candidate_or_policy_is_refused(
    tmp_path: Path, change: dict[str, str]
) -> None:
    root = tmp_path / "checkpoints"
    refused(
        "changed_candidate", root, retained(root), current=replace(BINDING, **change)
    )


def test_receipt_for_a_different_candidate_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    refused("changed_candidate", root, retained(root, receipt="other"))


def test_unbound_legacy_trace_is_not_resumable(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    refused("unbound_legacy_trace", root, retained(root, binding=None))


def test_write_stage_is_outside_the_first_continuation_scope(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    refused("write_stage", root, retained(root, read_only=False))


@pytest.mark.parametrize("sessions", [0, 2])
def test_ambiguous_native_sessions_are_refused(tmp_path: Path, sessions: int) -> None:
    root = tmp_path / "checkpoints"
    refused("ambiguous_state", root, retained(root, sessions=sessions))


def test_incompatible_harness_version_or_config_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    refused("incompatible_harness", root, profile, harness_version="1.19.0")
    refused("incompatible_harness", root, profile, config_sha256=sha256("other"))


@pytest.mark.parametrize("name", ["result.json", "candidate.json", "config.sha256"])
def test_missing_receipts_are_refused(tmp_path: Path, name: str) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    (profile / name).unlink()
    refused("missing_receipt", root, profile)


def test_unavailable_or_quarantined_candidate_receipt_is_refused(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkpoints"
    refused("missing_receipt", root, retained(root, receipt=None))
    profile = retained(root)
    receipt = json.loads((profile / "candidate.json").read_text(encoding="utf-8"))
    receipt["retention_errors"] = ["snapshot_git_error"]
    (profile / "candidate.json").write_text(json.dumps(receipt), encoding="utf-8")
    refused("missing_receipt", root, profile)


@pytest.mark.parametrize(
    "metadata",
    [
        "broken",
        "[]",
        json.dumps({"binding": {"run_id": 1}}),
        json.dumps({"binding": {**BINDING.__dict__, "run_id": "1"}, "read_only": True}),
    ],
)
def test_corrupt_metadata_is_refused(tmp_path: Path, metadata: str) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    (profile / "metadata.json").write_text(metadata, encoding="utf-8")
    refused("corrupt_state", root, profile)


@pytest.mark.parametrize(
    ("key", "value"), [("reasoning", "sometimes"), ("workspace", 7)]
)
def test_unknown_reasoning_or_workspace_is_refused(
    tmp_path: Path, key: str, value: object
) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    metadata = json.loads((profile / "metadata.json").read_text(encoding="utf-8"))
    metadata[key] = value
    (profile / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    refused("corrupt_state", root, profile)


def test_corrupt_native_database_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    (profile / "data/opencode/opencode.db").write_bytes(b"not a database")
    refused("corrupt_state", root, profile)


def test_paths_outside_the_controller_root_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    retained(root)
    outside = retained(tmp_path / "elsewhere")
    refused("path_escape", root, outside)
    refused("path_escape", root, root / ".." / "elsewhere" / outside.name)
    with pytest.raises(RefusedError, match="path_escape"):
        check(root, root / "missing")


def test_linked_profile_or_database_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    source = retained(root)
    link = root / "run-1-agent-linked"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra rights on this host")
    refused("path_escape", root, link)
    profile = retained(root)
    db = profile / "data/opencode/opencode.db"
    db.unlink()
    db.symlink_to(source / "data/opencode/opencode.db")
    refused("path_escape", root, profile)


@pytest.mark.parametrize("tool", ["edit", "write", "apply_patch", "batch", None])
def test_pending_write_tools_never_run_again(tmp_path: Path, tool: str | None) -> None:
    root = tmp_path / "checkpoints"
    part = {"type": "tool", "tool": tool, "state": {"status": "running"}}
    refused("pending_write", root, retained(root, parts=[part]))


def test_pending_reads_may_continue_under_the_read_only_policy(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    part = {"type": "tool", "tool": "read", "state": {"status": "pending"}}
    assert check(root, retained(root, parts=[part])).session_id == "ses_0"


def test_concurrent_continuation_is_refused_and_the_lock_is_released(
    tmp_path: Path,
) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    runner = Recorder(root)
    source = check(root, profile)
    with (
        continuation.exclusive(root, source),
        pytest.raises(RefusedError, match="continuation_active"),
    ):
        continuation.resume(
            worker(root),
            "continue",
            tmp_path,
            profile,
            current=lambda: (BINDING, True),
            harness_version=VERSION,
            steps=STEPS,
            runner=runner,
        )
    assert runner.calls == []
    continuation.resume(
        worker(root),
        "continue",
        tmp_path,
        profile,
        current=lambda: (BINDING, True),
        harness_version=VERSION,
        steps=STEPS,
        runner=runner,
    )
    assert runner.locked == [True]
    assert not any(path.suffix == ".lock" for path in root.iterdir())


@pytest.mark.parametrize(
    "after",
    [
        (replace(BINDING, patch_sha256="moved"), True),
        (replace(BINDING, task_sha256="edited"), True),
        (BINDING, False),
    ],
)
def test_stale_continuation_keeps_its_record_but_is_not_current(
    tmp_path: Path, after: tuple[CallBinding, bool]
) -> None:
    root = tmp_path / "checkpoints"
    profile = retained(root)
    views = iter([(BINDING, True), after])
    done = continuation.resume(
        worker(root),
        "continue",
        tmp_path,
        profile,
        current=lambda: next(views),
        harness_version=VERSION,
        steps=STEPS,
        runner=Recorder(root),
    )
    assert done.current is False
    assert done.result.text == "answer"
    assert (done.profile / "events.jsonl").is_file()


def test_resume_requires_retained_checkpoint_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="checkpoint"):
        continuation.resume(
            MiMoOpenCode(SecretStr("test-only")),
            "continue",
            tmp_path,
            tmp_path,
            current=lambda: (BINDING, True),
            harness_version=VERSION,
            steps=STEPS,
            runner=Recorder(tmp_path),
        )
