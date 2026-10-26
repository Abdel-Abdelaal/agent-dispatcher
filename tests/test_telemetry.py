import json
from datetime import UTC, datetime
from pathlib import Path

from agent_dispatcher.telemetry import (
    EXCERPT_CHARS,
    SCHEMA,
    Telemetry,
    bound,
    output_fields,
    sha256,
)

FIXED = datetime(2026, 10, 26, 9, tzinfo=UTC)


def lines(directory: Path) -> list[dict[str, object]]:
    path = directory / "attempts-2026-10.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_records_are_appended_to_a_monthly_file(tmp_path: Path) -> None:
    sink = Telemetry(tmp_path, clock=lambda: FIXED)
    sink.record(stage="plan", outcome="ok")
    sink.record(stage="review", outcome="approved")
    records = lines(tmp_path)
    assert [r["stage"] for r in records] == ["plan", "review"]
    assert all(r["schema"] == SCHEMA for r in records)
    assert records[0]["recorded_at"] == FIXED.isoformat()
    assert records[0]["record_id"] != records[1]["record_id"]


def test_long_strings_and_lists_are_capped() -> None:
    capped = bound({"text": "x" * 5000, "items": list(range(50)), "n": 3})
    assert len(str(capped["text"])) == EXCERPT_CHARS
    assert capped["items"] == list(range(20))
    assert capped["n"] == 3


def test_output_is_described_not_stored_whole() -> None:
    fields = output_fields("y" * 3000)
    assert fields["output_sha256"] == sha256("y" * 3000)
    assert fields["output_chars"] == 3000
    assert len(str(fields["output_excerpt"])) == EXCERPT_CHARS
