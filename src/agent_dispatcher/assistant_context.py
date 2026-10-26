"""Reviewed shared context and owner-approved local notes."""

from pathlib import Path


def context(root: Path) -> str:
    """Load reviewed context and owner-approved notes, never .env or raw handoffs."""
    paths = [
        root / "docs" / "assistant-context.md",
        root / "runs" / "assistant-notes.txt",
    ]
    return "\n\n".join(
        path.read_text(encoding="utf-8")[:8000] for path in paths if path.is_file()
    )
