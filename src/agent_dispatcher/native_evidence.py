"""Private native state, with a payload-free diagnostic view for reviewed tools."""

import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_dispatcher.telemetry import sha256


@dataclass(frozen=True)
class CallBinding:
    """Controller-authored identity of a call; not authorization to resume it."""

    run_id: int
    stage: str
    task_sha256: str
    base_commit: str
    patch_sha256: str
    policy_sha256: str


_TOOLS = {"read", "glob", "grep", "list", "edit", "write", "apply_patch", "batch"}
_STATES = {"pending", "running", "completed", "error"}
_CATEGORIES = {
    "stage": {
        "authorization",
        "route",
        "triage",
        "plan",
        "implement",
        "repair",
        "review",
        "ci_repair",
        "ci_review",
        "gate",
        "publish",
        "error",
    },
    "outcome": {
        "invalid_plan",
        "invalid_json",
        "declined",
        "ok",
        "failed",
        "timed_out",
        "exhausted",
        "max_turns",
        "turns_exhausted",
        "skipped",
        "proceed",
        "refused",
        "passed",
        "approved",
        "rejected",
        "error",
        "unavailable",
        "cycle_timeout",
        "cycle_abandoned",
        "controller_cancelled",
        "controller_failed",
    },
    "harness": {"opencode", "claude-code", "codex-cli", "chat-api", "cycle-supervisor"},
}


def metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Accept only known categories and numeric counters, never arbitrary strings."""
    result = {
        key: row.get(key) if row.get(key) in values else "other"
        for key, values in _CATEGORIES.items()
        if isinstance(row.get(key), str) or row.get(key) is None
    }
    for key in ("duration_ms", "exit_code"):
        result[key] = row.get(key) if type(row.get(key)) is int else None
    raw = row.get("usage")
    usage = cast("dict[str, Any]", raw) if isinstance(raw, dict) else None
    result["usage"] = (
        {
            key: value
            for key, value in usage.items()
            if key in {"input_tokens", "output_tokens", "reasoning_tokens"}
            and type(value) in {int, float}
        }
        if usage is not None
        else None
    )
    return result


def create(  # noqa: PLR0913 - optional controller binding accompanies native context
    root: Path,
    workspace: Path,
    prompt: str,
    *,
    read_only: bool,
    reasoning: str,
    binding: CallBinding | None = None,
) -> Path:
    """Retain local diagnostic context outside the model's editable worktree."""
    profile = root / (workspace.name + "-" + uuid.uuid4().hex)
    profile.mkdir(parents=True)
    (profile / "metadata.json").write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "started_at": datetime.now(UTC).isoformat(),
                "prompt_sha256": sha256(prompt),
                "read_only": read_only,
                "reasoning": reasoning,
                "binding": asdict(binding) if binding is not None else None,
            }
        ),
        encoding="utf-8",
    )
    return profile


def finish(profile: Path, code: int, output: str) -> None:
    """Keep private raw native events and a separate fixed-shape receipt."""
    (profile / "events.jsonl").write_text(output, encoding="utf-8")
    (profile / "result.json").write_text(
        json.dumps(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "exit_code": code,
                "output_sha256": sha256(output),
            }
        ),
        encoding="utf-8",
    )


def finish_candidate(profile: Path, patch_sha256: str | None) -> None:
    """Retain a separate final-state receipt without rewriting the native result."""
    with (profile / "candidate.json").open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "recorded_at": datetime.now(UTC).isoformat(),
                "status": "captured" if patch_sha256 is not None else "unavailable",
                "patch_sha256": patch_sha256,
            },
            handle,
        )


def inspect(profile: Path) -> dict[str, Any]:
    """Return only tool counters, never prompts, reasoning, paths or tool payloads."""
    result: dict[str, Any] = {"retained": True, "native_state": "unavailable"}
    try:
        metadata = json.loads((profile / "metadata.json").read_text(encoding="utf-8"))
        result["read_only"] = metadata.get("read_only") is True
        mode = metadata.get("reasoning")
        result["reasoning"] = mode if mode in {"enabled", "disabled"} else "unknown"
        path = profile / "data/opencode/opencode.db"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            parts = db.execute(
                "SELECT json_extract(data,'$.type'),count(*) FROM part GROUP BY 1"
            ).fetchall()
            tools = db.execute(
                "SELECT json_extract(data,'$.tool'),"
                "json_extract(data,'$.state.status'),"
                "count(*) FROM part WHERE json_extract(data,'$.type')='tool' "
                "GROUP BY 1,2"
            ).fetchall()
        counts: Counter[tuple[str, str]] = Counter()
        for tool, state, count in tools:
            counts[
                (
                    tool if tool in _TOOLS else "other",
                    state if state in _STATES else "unknown",
                )
            ] += count
        result.update(
            native_state="readable",
            steps_started=sum(n for kind, n in parts if kind == "step-start"),
            steps_finished=sum(n for kind, n in parts if kind == "step-finish"),
            tools=[
                {"name": name, "state": state, "count": n}
                for (name, state), n in sorted(counts.items())
            ],
        )
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        # Old/partial/corrupt state is not evidence of a completed native call.
        pass
    return result


def report(root: Path, run_id: int) -> dict[str, Any]:
    """Combine native counters and controller metrics without raw text payloads."""
    calls: list[dict[str, Any]] = []
    for path in sorted((root / "telemetry").glob("attempts-*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                row = cast("dict[str, Any]", row)
                if row.get("run_id") == run_id:
                    calls.append(metrics(row))
    return {
        "calls": calls,
        "native_evidence": [
            inspect(path)
            for pattern in (f"run-{run_id}-agent-*", f"run-{run_id}-publish-*-*")
            for path in sorted((root / "checkpoints").glob(pattern))
            if path.is_dir()
        ],
        "scope": "Local diagnostic metrics; raw private payloads are excluded.",
    }
