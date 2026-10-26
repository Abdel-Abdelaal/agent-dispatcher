"""Per-attempt records for the platform's signed AI telemetry.

The dispatcher only writes bounded, local JSONL. Redaction, signing and publishing
happen in the platform's own tooling, never in this process and never in a model's.
Prompts and full outputs are never stored: only hashes, sizes and a short excerpt.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "agent-dispatcher.attempt/v1"
EXCERPT_CHARS = 2000
MAX_LIST = 20


def sha256(text: str) -> str:
    """Return the hex SHA-256 of `text` as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Telemetry:
    """Appends one JSON line per attempt to a monthly file."""

    directory: Path
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def record(self, **fields: object) -> dict[str, object]:
        """Write one bounded record and return it."""
        now = self.clock()
        record: dict[str, object] = {
            "schema": SCHEMA,
            "record_id": str(uuid.uuid4()),
            "recorded_at": now.isoformat(),
            **bound(fields),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"attempts-{now:%Y-%m}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record


def bound(fields: Mapping[str, object]) -> dict[str, object]:
    """Cap strings and lists so no record can carry a whole prompt or log."""
    bounded: dict[str, object] = {}
    for key, value in fields.items():
        if isinstance(value, str) and len(value) > EXCERPT_CHARS:
            bounded[key] = value[:EXCERPT_CHARS]
        elif isinstance(value, list):
            bounded[key] = list(value)[:MAX_LIST]  # pyright: ignore[reportUnknownArgumentType]
        else:
            bounded[key] = value
    return bounded


def output_fields(text: str) -> dict[str, object]:
    """Describe a model's output by hash, size and a capped excerpt."""
    return {
        "output_sha256": sha256(text),
        "output_chars": len(text),
        "output_excerpt": text[:EXCERPT_CHARS],
    }
