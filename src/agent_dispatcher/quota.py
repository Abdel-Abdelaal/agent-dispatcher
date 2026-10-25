"""Quota tracking: park work before a subscription runs out, not after."""

import re
from dataclasses import dataclass
from datetime import datetime

# Phrases the CLIs and APIs use when an allowance is spent. Matching fails safe:
# an unrecognised failure is a failed run, never a reason to retry at once.
_EXHAUSTED = re.compile(
    r"usage limit|rate limit|quota (?:exceeded|exhausted)|limit reached"
    r"|insufficient_quota|\b429\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Quota:
    """What a provider has left. `used_share` is 0.0 to 1.0, or None if unknown."""

    provider: str
    used_share: float | None
    resets_at: datetime | None
    exhausted: bool = False


def allows(quota: Quota, max_share: float, now: datetime) -> bool:
    """Return True if a new run may start on this provider.

    The weekly cap leaves headroom for Abdel's own interactive use. An unknown
    share is allowed only because the run itself will report exhaustion.
    """
    if quota.resets_at is not None and now >= quota.resets_at:
        return True
    if quota.exhausted:
        return False
    return quota.used_share is None or quota.used_share < max_share


def looks_exhausted(output: str) -> bool:
    """Return True if a provider's error output says its allowance is spent."""
    return bool(_EXHAUSTED.search(output))
