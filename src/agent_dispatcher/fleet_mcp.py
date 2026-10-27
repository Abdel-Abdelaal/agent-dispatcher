"""Stdio MCP bridge to the existing controller, for the configured native UI only."""

import json
import os
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any, TextIO, cast

from pydantic import ValidationError

from agent_dispatcher.assistant_tools import ControllerTools, Request, ToolError, fail
from agent_dispatcher.providers import scrubbed_env, subprocess_runner
from agent_dispatcher.settings import load_settings
from agent_dispatcher.telemetry import Telemetry, redact, sha256

WRITES = frozenset({"queue_task", "create_repo", "start", "remember"})
DESCRIPTIONS = {
    "list_repos": "Discover owned public active repositories; no exact name needed.",
    "inspect_repo": "Inspect an owned public repo's description and default branch.",
    "read_file": "Read a bounded text file from an owned public repository.",
    "list_work": "List open issues and agent task labels in an owned public repo.",
    "inspect_pr": "Inspect an owned public PR's files and actual CI checks.",
    "search_repos": "Search public GitHub projects for reusable open-source work.",
    "status": "Read queued and running team tasks and provider availability.",
    "inspect_run": "Inspect local run metrics by repo/run number; no raw payloads.",
    "queue_task": "Publish a public ready task with title/text. Does not start it.",
    "create_repo": "Create an owned repo; private unless public explicitly chosen.",
    "start": "Start the MiMo-only team in the background for real coding work.",
    "remember": "Save an owner-requested local project note for future conversations.",
}
MAX_LINE = 64000


def definitions() -> list[dict[str, object]]:
    """Expose separate names so OpenCode can gate each write independently."""
    schema = Request.model_json_schema()
    del schema["properties"]["action"]
    schema.pop("required", None)
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": name not in WRITES,
                "idempotentHint": name not in WRITES,
                "destructiveHint": False,
                "openWorldHint": True,
            },
        }
        for name, description in DESCRIPTIONS.items()
    ]


class Bridge:
    """Handle bounded MCP requests without putting credentials in protocol replies."""

    def __init__(self, tools: ControllerTools, *, writes_enabled: bool = False) -> None:
        """Require a trusted native launcher to enable UI-approved writes."""
        self.tools, self.writes_enabled = tools, writes_enabled
        self.initialized = False
        self.receipts: dict[str, dict[str, object]] = {}
        self.audit = Telemetry(tools.root / "runs" / "assistant-telemetry")

    def execute(self, params: dict[str, Any]) -> dict[str, object]:
        """Validate the tool name/schema, then reuse the controller's policy gates."""
        action = params.get("name")
        signature = ""
        try:
            if action not in DESCRIPTIONS or (
                action in WRITES and not self.writes_enabled
            ):
                fail("Unknown tool or write disabled for this connection.")
            args = params.get("arguments", {})
            if not isinstance(args, dict) or "action" in args:
                fail("Invalid tool arguments.")
            request = Request.model_validate({**args, "action": action})
            signature = request.model_dump_json()
            if action in WRITES and signature in self.receipts:
                return self.receipts[signature]
            if redact(request.model_dump_json(), ())[1]:
                fail("Credential-like arguments are not accepted.")
            result = self.tools.call(request)
            self.audit.record(
                stage="native_tool",
                action=action,
                outcome="completed",
                input_sha256=sha256(request.model_dump_json()),
                output_sha256=sha256(result),
            )
            completed: dict[str, object] = {
                "content": [{"type": "text", "text": redact(result, ())[0][:32000]}]
            }
            if action in WRITES:
                self.receipts[signature] = completed
        except ToolError as error:
            message = str(error)
        except (ValidationError, ValueError, OSError, KeyError, TypeError):
            message = "Tool did not complete; inspect actual state before retrying."
        else:
            return completed
        safe_action = (
            action if isinstance(action, str) and action in DESCRIPTIONS else "invalid"
        )
        self.audit.record(stage="native_tool", action=safe_action, outcome="failed")
        failed: dict[str, object] = {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }
        if signature and action in WRITES:
            self.receipts[signature] = failed
        return failed

    def reply(self, message: dict[str, Any]) -> dict[str, object] | None:
        """Implement the MCP lifecycle, discovery and tool-call subset used here."""
        if "id" not in message:
            return None
        result: dict[str, object]
        method = message.get("method")
        if method == "initialize":
            self.initialized = True
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "abdel-fleet-controller", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32002, "message": "Initialize first."},
            }
        elif method == "tools/list":
            result = {"tools": definitions()}
        elif method == "tools/call":
            result = self.execute(message.get("params", {}))
        else:
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not available."},
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(bridge: Bridge, source: TextIO, destination: TextIO) -> int:
    """Use newline JSON only on stdout; oversized or malformed input fails closed."""
    while line := source.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE:
            return 1
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                return 1
            response = bridge.reply(cast("dict[str, Any]", value))
        except (ValueError, TypeError, KeyError):
            return 1
        if response is not None:
            destination.write(json.dumps(response) + "\n")
            destination.flush()
    return 0


def controller(root: Path, environ: Mapping[str, str]) -> ControllerTools:
    """Use the owner's existing gh login; no GitHub secret enters OpenCode config."""
    settings = load_settings(root / ".env")
    return ControllerTools(
        root,
        settings.owner,
        partial(subprocess_runner, timeout=45),
        scrubbed_env(environ),
        lambda _summary: True,
    )


def main() -> int:
    """Serve the reviewed local transport, with write approval owned by OpenCode UI."""
    root = Path(__file__).resolve().parents[2]
    bridge = Bridge(
        controller(root, os.environ), writes_enabled="--native-ui" in sys.argv
    )
    return serve(bridge, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
