"""Stdio MCP server giving one OpenCode call a `run` tool backed by the sandbox.

The model sends a shell command; it runs in a fresh locked-down container over
a copy of the call's worktree (see `sandbox`), and the bounded output comes
back. Nothing the command writes reaches the worktree: only file edits made
through OpenCode's own tools count. Every call is appended to a local log so
the controller can count and time them.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO, cast

from agent_dispatcher.sandbox import Sandbox

MAX_LINE = 64_000
MAX_COMMAND = 4_000
TOOL = {
    "name": "run",
    "description": (
        "Run a shell command (for example the project's tests, linter or type "
        "checker) in an isolated copy of the repository with its dependencies "
        "installed and no network. Returns the exit code and the output's end. "
        "Files the command writes are discarded; edit files with your edit tools."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"command": {"type": "string", "maxLength": MAX_COMMAND}},
        "required": ["command"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}


class Server:
    """Answer MCP requests for one worktree and one prepared image."""

    def __init__(
        self, sandbox: Sandbox, image: str, worktree: Path, log: Path | None
    ) -> None:
        """Bind the server to the call it serves."""
        self.sandbox, self.image, self.worktree, self.log = (
            sandbox,
            image,
            worktree,
            log,
        )
        self.initialized = False

    def _run(self, arguments: object) -> dict[str, object]:
        command = (
            cast("dict[str, Any]", arguments).get("command")
            if isinstance(arguments, dict)
            else None
        )
        if not isinstance(command, str) or not command.strip():
            return _text("Give a non-empty command.", error=True)
        if len(command) > MAX_COMMAND:
            return _text("The command is too long.", error=True)
        started = time.monotonic()
        result = self.sandbox.run(self.image, self.worktree, command)
        if self.log is not None:
            with self.log.open("a", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                            "seconds": round(time.monotonic() - started, 1),
                            "command_chars": len(command),
                        }
                    )
                    + "\n"
                )
        return _text(result.report())

    def reply(self, message: dict[str, Any]) -> dict[str, object] | None:
        """Implement the MCP lifecycle, discovery and the one tool."""
        if "id" not in message:
            return None
        method = message.get("method")
        result: dict[str, object]
        if method == "initialize":
            self.initialized = True
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-sandbox", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return _error(message["id"], -32002, "Initialize first.")
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            raw = message.get("params", {})
            params = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
            if params.get("name") != "run":
                result = _text("Unknown tool.", error=True)
            else:
                result = self._run(params.get("arguments"))
        else:
            return _error(message["id"], -32601, "Method not available.")
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def _text(text: str, *, error: bool = False) -> dict[str, object]:
    reply: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if error:
        reply["isError"] = True
    return reply


def _error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def serve(server: Server, source: TextIO, destination: TextIO) -> int:
    """Use newline JSON only on stdout; oversized or malformed input fails closed."""
    while line := source.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE:
            return 1
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                return 1
            response = server.reply(cast("dict[str, Any]", value))
        except (ValueError, TypeError, KeyError):
            return 1
        if response is not None:
            destination.write(json.dumps(response) + "\n")
            destination.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Serve one call: `--image`, `--worktree`, optional `--host`, `--key`, `--log`."""
    args = dict(zip(*[iter(sys.argv[1:] if argv is None else argv)] * 2, strict=True))
    sandbox = Sandbox(
        host=args.get("--host"),
        ssh_key=Path(args["--key"]) if "--key" in args else None,
    )
    log = Path(args["--log"]) if "--log" in args else None
    server = Server(sandbox, args["--image"], Path(args["--worktree"]), log)
    return serve(server, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
