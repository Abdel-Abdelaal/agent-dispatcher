"""Any OpenAI-compatible model editing files through the pinned OpenCode CLI.

MiMo was the first; an extra provider is the same harness with its own endpoint.
"""

import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
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


def records(output: str) -> Iterator[dict[str, Any]]:
    """Yield each JSON object in OpenCode's event stream, skipping anything else."""
    for line in output.splitlines():
        try:
            value: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield cast("dict[str, Any]", value)


def events(output: str) -> tuple[str, dict[str, int | float] | None, bool]:
    """Keep the last text reply and sum only explicit non-negative token counts."""
    text = ""
    usage: dict[str, int | float] = {}
    failed = False
    for data in records(output):
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
    return sum(data.get("type") == "step_finish" for data in records(output))


def finish_reason(output: str) -> str:
    """Return why the last model step ended ("stop", "tool-calls", "length"...).

    OpenCode ends the run when a step stops for any reason but tool calls, so
    the last step's reason is why the run ended. Empty when no step said.
    "length" is OpenCode's word for a reply cut off at the output limit.
    """
    reason = ""
    for data in records(output):
        part = data.get("part")
        if data.get("type") == "step_finish" and isinstance(part, dict):
            found = cast("dict[str, Any]", part).get("reason")
            reason = found if isinstance(found, str) else ""
    return reason


# A one-turn call's internal cap: above one so no last-step summary prompt.
ANSWER_STEPS = 2
# Variables the run tool's server keeps from the controller: none are secrets.
# Windows' own ssh.exe exits 255, silently, without PROGRAMDATA (where its
# system config lives), and the worker's scrubbed environment drops it.
_SERVER_ENV = (
    "SYSTEMROOT",
    "PROGRAMDATA",
    "USERPROFILE",
    "HOME",
    "TEMP",
    "TMP",
    "PATHEXT",
)


@dataclass(frozen=True)
class SandboxCall:
    """A prepared sandbox image that one call's `run` tool executes commands in."""

    image: str
    host: str | None = None
    ssh_key: Path | None = None
    log: Path | None = None
    client: str | None = None

    def server(self, worktree: Path) -> dict[str, object]:
        """Return OpenCode's config for the call's local sandbox MCP server."""
        args = ["--image", self.image, "--worktree", str(worktree)]
        if self.host is not None:
            args += ["--host", self.host]
        if self.ssh_key is not None:
            args += ["--key", str(self.ssh_key)]
        if self.client is not None:
            args += ["--client", self.client]
        if self.log is not None:
            args += ["--log", str(self.log)]
        return {
            "type": "local",
            "enabled": True,
            # -P and PYTHONSAFEPATH: OpenCode starts this in the worktree, and
            # Python would otherwise import a planted json.py from there, running
            # model-written code on this host.
            "command": [
                sys.executable,
                "-P",
                "-m",
                "agent_dispatcher.sandbox_mcp",
                *args,
            ],
            # OpenCode starts the server with only these variables: Windows
            # sockets need SYSTEMROOT, and ssh its profile folder.
            "environment": {
                **{
                    name: os.environ[name]
                    for name in _SERVER_ENV
                    if os.environ.get(name)
                },
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "PYTHONSAFEPATH": "1",
            },
            # A run waits for its container: a 300 s command plus the copy.
            "timeout": 420_000,
        }


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
    sandbox: SandboxCall | None = None

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

    def _instruction(self, *, read_only: bool, tools: bool) -> str:
        """The command-line message: what this call may do with its tools.

        A read-only planner or reviewer told to edit and fix spends its steps
        trying, so it gets its own wording.
        """
        if not tools:
            return (
                "Do the task in the attached file. You have no tools: answer from "
                "the attached text alone, in the requested format."
            )
        if read_only:
            return (
                "Do the task in the attached file. Edit nothing. "
                + (
                    "Use the sandbox run tool only to settle a specific doubt that "
                    "reading the code doesn't. "
                    if self.sandbox is not None
                    else "Use only the read tools; do not attempt shell commands. "
                )
                + "Finish with the requested answer."
            )
        return (
            "Do the task in the attached file. "
            + (
                "Edit with the file tools. Run the project's tests, linter and "
                "type checker with the sandbox run tool, the only shell you "
                "have, and fix what fails before you finish. "
                if self.sandbox is not None
                else "Use only available file tools. "
                "CI runs tests; do not attempt shell commands. "
            )
            + "Finish with the requested answer once the file work is complete."
        )

    def config(
        self,
        *,
        read_only: bool,
        steps: int,
        worktree: Path | None = None,
        tools: bool = True,
    ) -> str:
        """Deny ambient tools and project configuration; no shell or outside reads.

        With a sandbox, the one extra tool runs commands in its container, never
        on this host. Without tools every permission is denied, and OpenCode
        leaves denied tools out of the request.
        """
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
        if not tools:
            config["permission"] = {"*": "deny"}
        elif self.sandbox is not None and worktree is not None:
            config["mcp"] = {"sandbox": self.sandbox.server(worktree)}
            config["permission"]["sandbox_run"] = "allow"
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
        # One turn means answer now: no tools, so there's nothing to cut off. The
        # cap stays above one because OpenCode prefills a "summarise your work"
        # message at its last step, which would replace the requested answer.
        tools = steps > 1
        cap = steps if tools else ANSWER_STEPS
        config = self.config(
            read_only=read_only,
            steps=cap,
            worktree=worktree if tools else None,
            tools=tools,
        )
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
                self._instruction(read_only=read_only, tools=tools),
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
        if result.exit_code == 0 and steps_taken(stdout) >= cap:
            # OpenCode exits normally at its cap, after making the model summarise
            # without tools. Say so, as Claude Code does, so the controller can
            # continue a worker and knows a final reply may be incomplete.
            result = replace(result, exit_code=1, stop_reason="max_turns")
        elif result.exit_code == 0 and finish_reason(stdout) == "length":
            # The reply hit the output limit, often still reasoning, and OpenCode
            # ends there as if the model had finished. The run still counts as
            # finished; the controller decides what an empty result is worth.
            result = replace(result, stop_reason="output_limit")
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
