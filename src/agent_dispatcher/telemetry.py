"""Per-attempt records for the platform's signed AI telemetry.

The dispatcher only writes bounded, local JSONL. Signing and publishing happen in
the platform's own tooling, never in this process and never in a model's.

Privacy before retention: prompts and full outputs are never stored, only their
hashes and sizes. A model's output excerpt is untrusted and could echo a
credential, so known credentials and common token shapes are redacted first, and
the excerpt is kept only after a trusted scanner has passed it. Without a scanner,
the record says so explicitly and keeps only the hash and length.
"""

import hashlib
import json
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SCHEMA = "agent-dispatcher.attempt/v1"
SCHEMA_VERSION = 1
EXCERPT_CHARS = 2000
MAX_LIST = 20
REDACTED = "[REDACTED]"
_SCAN_TIMEOUT = 30
# Common credential shapes, in case a model echoes one we don't hold ourselves.
_TOKEN_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
# A held secret shorter than this is too likely to match ordinary text.
_MIN_SECRET = 8

Scanner = Callable[[str], str | None]


def sha256(text: str) -> str:
    """Return the hex SHA-256 of `text` as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact(text: str, secrets: Sequence[str]) -> tuple[str, bool]:
    """Replace held secrets and common token shapes; say whether anything changed."""
    cleaned = text
    for secret in secrets:
        if len(secret) >= _MIN_SECRET:
            cleaned = cleaned.replace(secret, REDACTED)
    cleaned = _TOKEN_SHAPES.sub(REDACTED, cleaned)
    return cleaned, cleaned != text


def command_scanner(path: Path) -> Scanner:
    """Return a scanner that pipes text through a trusted CLI (stdin to stdout).

    The CLI must exit 0 and print the scanned text; anything else means the excerpt
    is not kept.
    """

    def scan(text: str) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603 - a configured absolute path
                [str(path)],
                input=text.encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=_SCAN_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.decode("utf-8", errors="replace")

    return scan


def controller_revision(source: Path) -> str:
    """Return the controller's git commit, or "unknown" outside a checkout."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed git command
            ["git", "-C", str(source), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else "unknown"


@dataclass
class Telemetry:
    """Appends one JSON line per attempt to a monthly file with private permissions."""

    directory: Path
    secrets: Sequence[str] = ()
    scanner: Scanner | None = None
    revision: str = "unknown"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def output(self, text: str) -> dict[str, object]:
        """Describe a model's output: always hash and size, an excerpt only if safe."""
        truncated = len(text) > EXCERPT_CHARS
        described: dict[str, object] = {
            "output_sha256": sha256(text),
            "output_chars": len(text),
            "excerpt_truncated": truncated,
            "output_excerpt": None,
        }
        if not text:
            return described | {"excerpt_status": "empty"}
        if self.scanner is None:
            return described | {"excerpt_status": "omitted_no_scanner"}
        excerpt, redacted = redact(text[:EXCERPT_CHARS], self.secrets)
        scanned = self.scanner(excerpt)
        if scanned is None:
            return described | {"excerpt_status": "omitted_scanner_failed"}
        scanned, rescrubbed = redact(scanned, self.secrets)
        status = "redacted" if redacted or rescrubbed or scanned != excerpt else "clean"
        return described | {"excerpt_status": status, "output_excerpt": scanned}

    def record(self, **fields: object) -> dict[str, object]:
        """Write one bounded, redacted record and return it."""
        now = self.clock()
        record: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "record_id": str(uuid.uuid4()),
            "recorded_at": now.isoformat(),
            "controller_revision": self.revision,
            **bound(fields, self.secrets),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.directory / f"attempts-{now:%Y-%m}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        path.chmod(0o600)
        return record


def bound(
    fields: Mapping[str, object], secrets: Sequence[str] = ()
) -> dict[str, object]:
    """Cap strings and lists, and scrub held secrets from every string."""
    bounded: dict[str, object] = {}
    for key, value in fields.items():
        if isinstance(value, str):
            bounded[key] = redact(value[:EXCERPT_CHARS], secrets)[0]
        elif isinstance(value, list):
            items = cast("list[object]", value)[:MAX_LIST]
            bounded[key] = [
                redact(item, secrets)[0] if isinstance(item, str) else item
                for item in items
            ]
        else:
            bounded[key] = value
    return bounded
