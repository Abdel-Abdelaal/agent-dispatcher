"""Any OpenAI-compatible model editing files through the pinned OpenCode CLI.

MiMo was the first; an extra provider is the same harness with its own endpoint.
"""

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import SecretStr

from agent_dispatcher import native_evidence
from agent_dispatcher.providers import (
    MIMO_MODEL,
    MIMO_OPENAI,
    OpenCodeCli,
    Runner,
    RunResult,
    Tuning,
    mimo_native_model,
    provider_result,
    scrubbed_env,
    subprocess_runner,
)
from agent_dispatcher.workspace import GitError


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


def steps_taken(output: str) -> int:
    """Count the model steps OpenCode finished, from its JSON event stream."""
    count = 0
    for line in output.splitlines():
        try:
            value: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            data = cast("dict[str, Any]", value)
            count += data.get("type") == "step_finish"
    return count


@dataclass(frozen=True)
class OpenCodeWorker:
    """One model behind the same controller gates, explicit limits, ephemeral state.

    The defaults are MiMo's. `model_settings` replaces MiMo's reasoning and limits
    for any other model; OpenCode compacts the conversation against its limits.
    """

    api_key: SecretStr
    executable: str = "opencode"
    name: str = "mimo"
    reasoning: Literal["enabled", "disabled"] = "enabled"
    base_url: str = MIMO_OPENAI
    model: str = MIMO_MODEL
    model_settings: Mapping[str, object] | None = None
    checkpoint_root: Path | None = None
    binding: native_evidence.CallBinding | None = None
    candidate_snapshot: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        """Refuse a snapshot callback that cannot retain its receipt."""
        if self.candidate_snapshot is not None and self.checkpoint_root is None:
            message = "Candidate snapshots require retained checkpoint state."
            raise ValueError(message)

    def _retain(self, profile: Path, code: int, output: str) -> tuple[str, ...]:
        """Quarantine retention failures without losing the provider's result."""
        errors: list[str] = []
        try:
            native_evidence.finish(profile, code, output)
        except OSError:
            errors.append("native_storage_error")
        if self.candidate_snapshot is None:
            return tuple(errors)
        candidate = None
        try:
            candidate = self.candidate_snapshot()
        except GitError:
            errors.append("snapshot_git_error")
        except OSError:
            errors.append("snapshot_os_error")
        except Exception:  # noqa: BLE001 - a callback failure cannot discard usage/history
            errors.append("snapshot_callback_error")
        try:
            native_evidence.finish_candidate(profile, candidate, errors=tuple(errors))
        except OSError:
            errors.append("receipt_storage_error")
        return tuple(errors)

    def config(self, *, read_only: bool, steps: int) -> str:
        """Deny ambient tools and project configuration; no shell or outside reads."""
        config = cast(
            "dict[str, Any]",
            json.loads(
                OpenCodeCli(self.name, self.base_url, self.model, self.api_key).config(
                    read_only=read_only
                )
            ),
        )
        config.update(
            enabled_providers=["worker"],
            model="worker/" + self.model,
            small_model="worker/" + self.model,
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
            compaction={"auto": True, "prune": True},
        )
        model = config["provider"]["worker"]["models"][self.model]
        model.update(
            mimo_native_model(self.reasoning)
            if self.model_settings is None
            else self.model_settings
        )
        # Explicitly override built-in agent defaults as well as global policy.
        # Otherwise a built-in permission (for example questions) can take precedence.
        config["agent"]["build"]["permission"] = config["permission"]
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
        """Isolate ambient config; optionally retain private native diagnostic state."""
        del tuning
        return self._call(
            prompt,
            worktree,
            read_only=read_only,
            steps=max_turns or 15,
            runner=runner,
            environ=environ,
        )

    def resume(  # noqa: PLR0913 - the verified fork rides along with the call
        self,
        prompt: str,
        worktree: Path,
        *,
        profile: Path,
        session: str,
        steps: int,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Continue a forked copy of a verified retained session, read-only.

        Only `continuation.resume` should call this: it checks the source and makes
        the fork. OpenCode forks the session again inside the copy, so neither the
        retained original nor the copied original session is changed.
        """
        return self._call(
            prompt,
            worktree,
            read_only=True,
            steps=steps,
            runner=runner,
            environ=environ,
            fork=(profile, session),
        )

    def _call(  # noqa: PLR0913 - one native call, fresh or continued
        self,
        prompt: str,
        worktree: Path,
        *,
        read_only: bool,
        steps: int,
        runner: Runner,
        environ: Mapping[str, str] | None,
        fork: tuple[Path, str] | None = None,
    ) -> RunResult:
        config = self.config(read_only=read_only, steps=steps)
        retention_errors: tuple[str, ...] = ()
        with tempfile.TemporaryDirectory(prefix=f"{self.name}-opencode-") as tmp:
            root = Path(tmp)
            evidence = root
            if fork is not None:
                evidence = fork[0]
            elif self.checkpoint_root is not None:
                evidence = native_evidence.create(
                    self.checkpoint_root,
                    worktree,
                    prompt,
                    read_only=read_only,
                    reasoning=self.reasoning,
                    binding=self.binding,
                )
            if fork is not None or self.checkpoint_root is not None:
                (evidence / "config.sha256").write_text(
                    native_evidence.sha256(config),
                    encoding="utf-8",
                )
            task = evidence / "task.md"
            task.write_text(prompt, encoding="utf-8")
            env = scrubbed_env(os.environ if environ is None else environ)
            for key in ("CONFIG", "DATA", "CACHE", "STATE"):
                env[f"XDG_{key}_HOME"] = str(root / key.lower())
            env["XDG_DATA_HOME"] = str(evidence / "data")
            (root / "tmp").mkdir()
            env["TEMP"] = env["TMP"] = str(root / "tmp")
            env.update(
                OPENCODE_CONFIG_CONTENT=config,
                OPENCODE_DISABLE_PROJECT_CONFIG="1",
                OPENCODE_TEST_HOME=str(root / "home"),
                OPENCODE_DISABLE_AUTOUPDATE="1",
                OPENCODE_DISABLE_MODELS_FETCH="1",
                WORKER_API_KEY=self.api_key.get_secret_value(),
            )
            argv = [
                self.executable,
                "run",
                "--title",
                "Agent task",
                "--agent",
                "build",
                "--format",
                "json",
                "--model",
                f"worker/{self.model}",
                *(("--session", fork[1], "--fork") if fork is not None else ()),
                "--file",
                str(task),
                "--",
                "Do the task in the attached file. Use only available file tools. "
                "CI runs tests; do not attempt shell commands. "
                "Finish with the requested answer once the file work is complete.",
            ]
            code, stdout, stderr = runner(argv, worktree, "", env)
            if fork is not None or self.checkpoint_root is not None:
                retention_errors = self._retain(evidence, code, stdout)
        text, usage, failed = events(stdout)
        result = provider_result(
            self.name,
            code or int(failed),
            stdout,
            text,
            stderr or stdout,
            harness="opencode",
            tuning=Tuning(self.model, ""),
            usage=usage,
        )
        if result.exit_code == 0 and steps_taken(stdout) >= steps:
            # OpenCode exits normally at its cap, after making the model summarise
            # without tools. Say so, as Claude Code does, so the controller can
            # continue a worker and knows a final reply may be incomplete.
            result = replace(result, exit_code=1, stop_reason="max_turns")
        return (
            replace(
                result,
                exit_code=result.exit_code or 1,
                stop_reason=(
                    "timed_out"
                    if result.stop_reason == "timed_out"
                    else "candidate_unavailable"
                ),
                retention_errors=retention_errors,
            )
            if retention_errors
            else result
        )
