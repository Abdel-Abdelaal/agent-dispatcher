"""Owner-only intake and issue-text sanitising.

Issue text reaches a model, so it is treated as an injection channel: hidden
markup is removed and what remains is passed inside a delimited untrusted block.
"""

import re
import unicodedata
from dataclasses import dataclass

from agent_dispatcher.states import State

# Zero-width characters and bidi controls can hide instructions from a human reader.
# Built from code points so the source itself contains no invisible characters.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
)
_INVISIBLE = re.compile(
    "["
    + "".join(f"{chr(first)}-{chr(last)}" for first, last in _INVISIBLE_RANGES)
    + "]"
)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_DETAILS = re.compile(r"<details\b.*?(?:</details>|$)", re.DOTALL | re.IGNORECASE)
_FENCE = "<<<UNTRUSTED_ISSUE>>>"
_FENCE_END = "<<<END_UNTRUSTED_ISSUE>>>"


@dataclass(frozen=True)
class Issue:
    """An open issue as the controller sees it."""

    repo: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    labelled_by: str | None


def sanitise(text: str) -> str:
    """Remove markup a reader can't see, and anything that mimics our fences."""
    text = unicodedata.normalize("NFKC", text)
    text = _HTML_COMMENT.sub("", text)
    text = _DETAILS.sub("", text)
    text = _INVISIBLE.sub("", text)
    return text.replace(_FENCE, "").replace(_FENCE_END, "").strip()


def untrusted_block(issue: Issue) -> str:
    """Return the issue as data for a prompt, never as instructions."""
    return (
        f"{_FENCE}\n"
        f"title: {sanitise(issue.title)}\n\n"
        f"{sanitise(issue.body)}\n"
        f"{_FENCE_END}"
    )


def accepts(issue: Issue, owner: str) -> bool:
    """Return True only if the owner applied the ready label.

    Anyone with triage access can label an issue, and anyone can write its body,
    so authorship is not enough: the ready label itself must come from the owner.
    """
    return State.READY.value in issue.labels and issue.labelled_by == owner
