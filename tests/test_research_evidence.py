"""Keep the public experiment manifest bound to its published evidence."""

import hashlib
import json
from pathlib import Path
from typing import cast


def test_research_evidence_manifest_matches_public_files():
    root = Path(__file__).resolve().parents[1] / "docs/evaluation-2026-10-30"
    manifest = cast(
        "dict[str, object]",
        json.loads((root / "manifest.json").read_text(encoding="utf-8")),
    )
    hashes = cast("dict[str, str]", manifest["sha256"])
    assert "experiment-plan.json" in hashes
    assert "continuation-acceptance.json" in hashes
    for name, expected in hashes.items():
        assert Path(name).name == name
        # Git publishes LF; universal-newline reading also works in CRLF checkouts.
        content = (root / name).read_text(encoding="utf-8").encode("utf-8")
        assert hashlib.sha256(content).hexdigest() == expected, name
