"""Configuration, read once from `DISPATCHER_` environment variables."""

from copy import deepcopy
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_dispatcher.levels import DEFAULT_LADDER


def _split_repos(value: object) -> object:
    """Accept `owner/a,owner/b` from the environment."""
    if isinstance(value, str):
        return [repo.strip() for repo in value.split(",") if repo.strip()]
    return value


ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
CodexEffort = Literal["minimal", "low", "medium", "high", "xhigh"]


class Settings(BaseSettings):
    """Everything the controller can be told; secrets stay `SecretStr`."""

    model_config = SettingsConfigDict(env_prefix="DISPATCHER_", env_file=".env")

    owner: str = "Abdel-Abdelaal"
    repos: Annotated[list[str], NoDecode, BeforeValidator(_split_repos)] = Field(
        default_factory=lambda: ["Abdel-Abdelaal/shortlink"]
    )
    # "auto" discovers every public, non-fork, non-archived repository the owner
    # has, so new repositories join without a settings change.
    exclude_repos: Annotated[list[str], NoDecode, BeforeValidator(_split_repos)] = (
        Field(default_factory=lambda: ["Abdel-Abdelaal/agent-dispatcher"])
    )
    db_path: Path = Path("dispatcher.sqlite3")
    max_weekly_share: float = Field(default=0.6, gt=0, le=1)
    max_files: int = Field(default=15, gt=0)
    max_lines: int = Field(default=400, gt=0)
    work_root: Path = Path("runs")
    park_hours: float = Field(default=5, gt=0)
    allow_worker_thinking: bool = False
    disabled_providers: Annotated[
        list[str], NoDecode, BeforeValidator(_split_repos)
    ] = Field(default_factory=list)
    github_token: SecretStr | None = None
    claude_oauth_token: SecretStr | None = None
    # A trusted CLI (stdin to stdout) that scans excerpts before they're kept.
    # Without it, telemetry keeps only each output's hash and length.
    output_scanner: Path | None = None
    # Experiment: let easy documentation tasks go straight to the worker. The
    # review still checks the change. Compare telemetry before keeping it on.
    skip_plan_for_easy_docs: bool = False
    # Thinking (plan, review) follows the task's level: see levels.DEFAULT_LADDER.
    # Override with JSON, e.g. {"claude": {"hard": ["claude-opus-5-5", "max"]}}.
    thinking_ladder: dict[str, dict[str, tuple[str, str]]] = Field(
        default_factory=lambda: deepcopy(DEFAULT_LADDER)
    )
    # Implementing, which a thinker only does when both workers are out, gets a
    # lighter fixed setting to save the scarce allowance.
    claude_work_model: str = "claude-sonnet-5-5"
    claude_work_effort: ClaudeEffort = "medium"
    codex_work_model: str = "gpt-6.1-sol"
    codex_work_effort: CodexEffort = "medium"
    mimo_api_key: SecretStr | None = None
    mimo_harness: Literal["claude-code", "opencode"] = "claude-code"
    opencode_executable: str = "opencode"
    nvidia_api_key: SecretStr | None = None
