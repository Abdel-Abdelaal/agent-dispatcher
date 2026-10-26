"""Opt-in MiMo harness comparison using the already pinned OpenCode CLI."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import SecretStr

from agent_dispatcher.providers import (
    MIMO_MODEL,
    MIMO_OPENAI,
    OpenCodeCli,
    Runner,
    RunResult,
    Tuning,
    provider_result,
    scrubbed_env,
    subprocess_runner,
)


def events(output: str) -> tuple[str, dict[str, int | float] | None, bool]:
    """Keep the last text reply and sum only explicit non-negative token counts."""
    text = ""
    usage: dict[str, int | float] = {}
    failed = False
    for line in output.splitlines():
        try:
            value: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        data = cast("dict[str, Any]", value)
        part = data.get("part", {})
        if data.get("type") == "error":
            failed = True
        if not isinstance(part, dict):
            continue
        part = cast("dict[str, Any]", part)
        if data.get("type") == "text" and isinstance(part.get("text"), str):
            text = part["text"]
        tokens = part.get("tokens")
        if data.get("type") == "step_finish" and isinstance(tokens, dict):
            tokens = cast("dict[str, Any]", tokens)
            for key in ("input", "output", "reasoning"):
                count = tokens.get(key)
                if type(count) is int and count >= 0:
                    name = key + "_tokens"
                    usage[name] = usage.get(name, 0) + count
    return text, usage or None, failed


@dataclass(frozen=True)
class MiMoOpenCode:
    """Same MiMo model and controller gates, explicit limits and ephemeral state."""

    api_key: SecretStr
    executable: str = "opencode"
    name: str = "mimo"

    def config(self, *, read_only: bool, steps: int) -> str:
        """Deny ambient tools and project configuration; no shell or outside reads."""
        config = cast(
            "dict[str, Any]",
            json.loads(
                OpenCodeCli(self.name, MIMO_OPENAI, MIMO_MODEL, self.api_key).config(
                    read_only=read_only
                )
            ),
        )
        config.update(
            permission={
                "*": "deny",
                "read": {
                    "*": "allow",
                    "*.env": "deny",
                    "*.env.*": "deny",
                    "*.env.example": "allow",
                },
                "glob": "allow",
                "grep": "allow",
                "list": "allow",
                "edit": "deny" if read_only else "allow",
                "external_directory": "deny",
            },
            agent={"build": {"steps": steps}},
            mcp={},
            plugin=[],
            lsp=False,
            formatter=False,
        )
        return json.dumps(config)

    def run(  # noqa: PLR0913 - matches the controller's provider protocol
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool = False,
        tuning: Tuning | None = None,
        max_turns: int | None = None,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run with no user config, persistent transcripts or prompt arguments."""
        del tuning
        with tempfile.TemporaryDirectory(prefix="mimo-opencode-") as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(prompt, encoding="utf-8")
            env = scrubbed_env(os.environ if environ is None else environ)
            for key in ("CONFIG", "DATA", "CACHE", "STATE"):
                env[f"XDG_{key}_HOME"] = str(root / key.lower())
            (root / "tmp").mkdir()
            env["TEMP"] = env["TMP"] = str(root / "tmp")
            env.update(
                OPENCODE_CONFIG_CONTENT=self.config(
                    read_only=read_only, steps=max_turns or 15
                ),
                OPENCODE_DISABLE_PROJECT_CONFIG="1",
                OPENCODE_TEST_HOME=str(root / "home"),
                OPENCODE_DISABLE_AUTOUPDATE="1",
                OPENCODE_DISABLE_MODELS_FETCH="1",
                WORKER_API_KEY=self.api_key.get_secret_value(),
            )
            argv = [
                self.executable,
                "run",
                "--agent",
                "build",
                "--format",
                "json",
                "--model",
                f"worker/{MIMO_MODEL}",
                "--file",
                str(task),
                "--",
                "Do the task in the attached file. Use only available file tools. "
                "CI runs tests; do not attempt shell commands. "
                "Finish with the requested answer once the file work is complete.",
            ]
            code, stdout, stderr = runner(argv, worktree, "", env)
        text, usage, failed = events(stdout)
        return provider_result(
            self.name,
            code or int(failed),
            stdout,
            text,
            stderr or stdout,
            harness="opencode",
            tuning=Tuning(MIMO_MODEL, ""),
            usage=usage,
        )
