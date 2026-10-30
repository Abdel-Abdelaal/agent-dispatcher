import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_dispatcher import cli, native_evidence
from agent_dispatcher.queue import RunQueue, Task
from agent_dispatcher.settings import Settings


def native_db(profile: Path) -> None:
    path = profile / "data/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    parts = [
        {"type": "step-start"},
        {"type": "step-start"},
        {"type": "step-finish"},
        {"type": "reasoning", "text": "confidential-payload"},
        {
            "type": "tool",
            "tool": "read",
            "state": {"status": "completed", "input": "confidential-payload"},
        },
        {"type": "tool", "tool": "read", "state": {"status": "error"}},
        {
            "type": "tool",
            "tool": "confidential-payload",
            "state": {"status": "confidential-payload"},
        },
    ]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE part(data TEXT)")
        db.executemany("INSERT INTO part VALUES(?)", [(json.dumps(x),) for x in parts])


def test_partial_native_state_returns_counters_without_confidential_payloads(
    tmp_path: Path,
) -> None:
    profile = native_evidence.create(
        tmp_path,
        tmp_path / "run-1-agent",
        "confidential-payload",
        read_only=True,
        reasoning="enabled",
    )
    assert json.loads((profile / "metadata.json").read_text())["binding"] is None
    native_db(profile)
    native_evidence.finish(profile, 124, "confidential-payload")
    result = native_evidence.inspect(profile)
    assert result["native_state"] == "readable"
    assert result["steps_started"] == 2
    assert result["steps_finished"] == 1
    assert result["read_only"] is True
    assert result["reasoning"] == "enabled"
    assert result["tools"] == [
        {"name": "other", "state": "unknown", "count": 1},
        {"name": "read", "state": "completed", "count": 1},
        {"name": "read", "state": "error", "count": 1},
    ]
    assert "confidential-payload" not in json.dumps(result)
    assert (profile / "events.jsonl").read_text() == "confidential-payload"


@pytest.mark.parametrize("metadata", [None, "broken", "[]", '{"reasoning":[]}'])
def test_missing_corrupt_or_partial_state_is_never_a_success(
    tmp_path: Path, metadata: str | None
) -> None:
    if metadata is not None:
        (tmp_path / "metadata.json").write_text(metadata)
    assert native_evidence.inspect(tmp_path)["native_state"] == "unavailable"


def test_report_filters_other_runs_and_excludes_payloads_and_untrusted_categories(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "telemetry"
    logs.mkdir()
    rows = [
        "not JSON",
        "[]",
        json.dumps(
            {"run_id": 2, "stage": "private-repo", "output": "confidential-payload"}
        ),
        json.dumps(
            {
                "run_id": 1,
                "stage": "plan",
                "outcome": "timed_out",
                "harness": "opencode",
                "duration_ms": 12,
                "exit_code": 124,
                "output": "confidential-payload",
                "usage": {"reasoning_tokens": 7, "untrusted": "confidential-payload"},
            }
        ),
        json.dumps(
            {
                "run_id": 1,
                "stage": "confidential-payload",
                "outcome": [],
                "harness": None,
                "duration_ms": "confidential-payload",
            }
        ),
    ]
    (logs / "attempts-test.jsonl").write_text("\n".join(rows))
    result = native_evidence.report(tmp_path, 1)
    assert len(result["calls"]) == 2
    assert result["calls"][0]["usage"] == {"reasoning_tokens": 7}
    assert result["calls"][1]["stage"] == "other"
    assert result["calls"][1]["duration_ms"] is None
    assert result["native_evidence"] == []
    assert "confidential-payload" not in json.dumps(result)
    assert "private-repo" not in json.dumps(result)


def test_cli_evidence_checks_repo_before_reading_and_handles_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=tmp_path / "queue.sqlite3", work_root=tmp_path)
    queue = RunQueue(settings.db_path)
    run_id = queue.start_run(Task("owner/app", 5, "task"), "mimo", datetime.now(UTC))
    queue.finish_run(run_id, "failed", datetime.now(UTC))
    queue.close()
    report = Mock(return_value={"native_evidence": []})
    monkeypatch.setattr(cli, "evidence_report", report)
    monkeypatch.setattr(cli, "Settings", Mock(return_value=settings))
    assert cli.main(["evidence", str(run_id), "--repo", "other/app"]) == 1
    assert cli.show_evidence(settings, "owner/app", 999) == 1
    report.assert_not_called()
    assert cli.main(["evidence", str(run_id), "--repo", "owner/app"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == run_id
    assert data["outcome"] == "failed"
    assert data["issue"] == 5


def test_report_includes_review_attempts_without_neighbour_runs_or_payloads(
    tmp_path: Path,
) -> None:
    for name, read_only in [
        ("run-1-agent", True),
        ("run-1-agent", False),
        ("run-1-publish-1", True),
        ("run-1-publish-2", True),
        ("run-10-publish-1", True),
    ]:
        profile = native_evidence.create(
            tmp_path / "checkpoints",
            tmp_path / name,
            "confidential-payload",
            read_only=read_only,
            reasoning="enabled",
        )
        native_db(profile)
    result = native_evidence.report(tmp_path, 1)
    assert len(result["native_evidence"]) == 4
    assert all(r["native_state"] == "readable" for r in result["native_evidence"])
    assert "confidential-payload" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
