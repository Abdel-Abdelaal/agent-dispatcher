import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_dispatcher.telemetry import (
    EXCERPT_CHARS,
    REDACTED,
    SCHEMA,
    SCHEMA_VERSION,
    Telemetry,
    bound,
    command_scanner,
    controller_revision,
    redact,
    sha256,
)

FIXED = datetime(2026, 10, 26, 9, tzinfo=UTC)
HELD = "mimo-secret-value-123456"


def lines(directory: Path) -> list[dict[str, object]]:
    path = directory / "attempts-2026-10.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def passthrough(text: str) -> str:
    return text


def test_records_carry_schema_version_and_revision(tmp_path: Path) -> None:
    sink = Telemetry(tmp_path, revision="abc123", clock=lambda: FIXED)
    sink.record(stage="plan", outcome="ok")
    sink.record(stage="review", outcome="approved")
    records = lines(tmp_path)
    assert [r["stage"] for r in records] == ["plan", "review"]
    assert all(r["schema"] == SCHEMA for r in records)
    assert all(r["schema_version"] == SCHEMA_VERSION for r in records)
    assert all(r["controller_revision"] == "abc123" for r in records)
    assert records[0]["record_id"] != records[1]["record_id"]


def test_held_secrets_never_reach_the_file(tmp_path: Path) -> None:
    sink = Telemetry(tmp_path, secrets=[HELD], scanner=passthrough, clock=lambda: FIXED)
    sink.record(stage="plan", reason=f"key={HELD}", files=[f"x{HELD}"])
    sink.record(stage="plan", **sink.output(f"the key is {HELD}"))
    text = (tmp_path / "attempts-2026-10.jsonl").read_text(encoding="utf-8")
    assert HELD not in text
    assert lines(tmp_path)[1]["excerpt_status"] == "redacted"


def test_common_token_shapes_are_redacted() -> None:
    token = "ghp_" + "a" * 36
    cleaned, changed = redact(f"use {token} now", [])
    assert (cleaned, changed) == (f"use {REDACTED} now", True)
    key = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(key, [])[0] == REDACTED
    assert redact("short", ["abc"]) == ("short", False)


def test_without_a_scanner_only_hash_and_length_are_kept() -> None:
    sink = Telemetry(Path("unused"))
    described = sink.output("y" * 3000)
    assert described == {
        "output_sha256": sha256("y" * 3000),
        "output_chars": 3000,
        "excerpt_truncated": True,
        "output_excerpt": None,
        "excerpt_status": "omitted_no_scanner",
    }
    assert sink.output("")["excerpt_status"] == "empty"


def test_a_scanner_decides_what_is_kept() -> None:
    clean = Telemetry(Path("unused"), scanner=passthrough).output("plain words")
    assert (clean["excerpt_status"], clean["output_excerpt"]) == (
        "clean",
        "plain words",
    )
    assert clean["excerpt_truncated"] is False
    masked = Telemetry(
        Path("unused"), scanner=lambda text: text.replace("words", "*")
    ).output("plain words")
    assert (masked["excerpt_status"], masked["output_excerpt"]) == (
        "redacted",
        "plain *",
    )
    failed = Telemetry(Path("unused"), scanner=lambda _text: None).output("plain words")
    assert (failed["excerpt_status"], failed["output_excerpt"]) == (
        "omitted_scanner_failed",
        None,
    )
    long = Telemetry(Path("unused"), scanner=passthrough).output("z" * 5000)
    assert len(str(long["output_excerpt"])) == EXCERPT_CHARS


def test_command_scanner_runs_a_trusted_cli(tmp_path: Path) -> None:
    script = tmp_path / "scan.py"
    script.write_text(
        "import sys; sys.stdout.write(sys.stdin.read().upper())", encoding="utf-8"
    )
    runner = tmp_path / ("scan.cmd" if sys.platform == "win32" else "scan.sh")
    if sys.platform == "win32":
        runner.write_text(f'@"{sys.executable}" "{script}"\n', encoding="utf-8")
    else:
        runner.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8"
        )
        runner.chmod(0o755)
    assert command_scanner(runner)("abc") == "ABC"
    assert command_scanner(tmp_path / "missing")("abc") is None
    failing = tmp_path / ("fail.cmd" if sys.platform == "win32" else "fail.sh")
    failing.write_text(
        "exit 3\n" if sys.platform == "win32" else "#!/bin/sh\nexit 3\n",
        encoding="utf-8",
    )
    failing.chmod(0o755)
    assert command_scanner(failing)("abc") is None


def test_controller_revision_reads_git_or_says_unknown(tmp_path: Path) -> None:
    here = controller_revision(Path(__file__).parent)
    assert len(here) == 40 or here == "unknown"
    assert controller_revision(tmp_path) == "unknown"


def test_long_strings_and_lists_are_capped() -> None:
    capped = bound({"text": "x" * 5000, "items": list(range(50)), "n": 3})
    assert len(str(capped["text"])) == EXCERPT_CHARS
    assert capped["items"] == list(range(20))
    assert capped["n"] == 3


def test_controller_revision_survives_a_missing_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_git(*_: object, **__: object) -> object:
        raise OSError

    monkeypatch.setattr(subprocess, "run", no_git)
    assert controller_revision(tmp_path) == "unknown"
