"""Configuration, read once from `DISPATCHER_` environment variables."""

from copy import deepcopy
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_dispatcher.levels import DEFAULT_LADDER


def load_settings(path: Path) -> "Settings":
    """Load the controller file independently of an MCP client's working directory."""
    # Pydantic supports this runtime option; its generated signature omits it.
    return Settings(_env_file=path)  # pyright: ignore[reportCallIssue]


def _split_repos(value: object) -> object:
    """Accept `owner/a,owner/b` from the environment."""
    if isinstance(value, str):
        return [repo.strip() for repo in value.split(",") if repo.strip()]
    return value


def _empty_is_none(value: object) -> object:
    """Treat an empty variable as unset, as `.env.example` leaves it."""
    return None if value == "" else value


def _absolute(path: Path | None) -> Path | None:
    """Refuse a relative path, which would depend on the working directory."""
    if path is not None and not path.is_absolute():
        message = f"must be an absolute path, got {path}"
        raise ValueError(message)
    return path


ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
CodexEffort = Literal["minimal", "low", "medium", "high", "xhigh"]
BUILT_IN_PROVIDERS = ("claude", "codex", "mimo", "nvidia")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _not_built_in(name: str) -> str:
    """Keep built-in names unambiguous in labels, parking and telemetry."""
    if name in BUILT_IN_PROVIDERS:
        message = f"{name} is a built-in provider"
        raise ValueError(message)
    return name


def _encrypted_unless_local(url: str) -> str:
    """Refuse to send a key or code over plain HTTP, except to this machine."""
    parsed = urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
    if not parsed.hostname or not (parsed.scheme == "https" or local):
        message = "base_url must be https, or http to localhost, 127.0.0.1 or ::1"
        raise ValueError(message)
    return url.rstrip("/")


class ExtraProvider(BaseModel):
    """Any OpenAI-compatible endpoint, driven through the same OpenCode harness.

    OpenRouter, Groq, DeepSeek, a local Ollama or LM Studio server, or a LiteLLM
    proxy in front of anything else: no adapter code, only this entry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9-]{1,30}$"), AfterValidator(_not_built_in)
    ]
    base_url: Annotated[str, AfterValidator(_encrypted_unless_local)]
    model: str = Field(min_length=1)
    # A local server ignores it, but OpenCode still sends one: any value works.
    api_key: SecretStr
    # Workers implement; a thinker may also plan and review.
    role: Literal["worker", "thinker"] = "worker"
    # OpenCode compacts the conversation against these, so give the real limits.
    context: int = Field(default=128_000, gt=0)
    output: int = Field(default=16_000, gt=0)


def _unique_names(providers: list[ExtraProvider]) -> list[ExtraProvider]:
    names = [provider.name for provider in providers]
    if len(names) != len(set(names)):
        message = "extra provider names must be unique"
        raise ValueError(message)
    return providers


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
    cycle_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    run_lease_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    allow_worker_thinking: bool = False
    disabled_providers: Annotated[
        list[str], NoDecode, BeforeValidator(_split_repos)
    ] = Field(default_factory=list)
    github_token: SecretStr | None = None
    claude_oauth_token: SecretStr | None = None
    # A trusted CLI (stdin to stdout) that scans excerpts before they're kept.
    # Without it, telemetry keeps only each output's hash and length.
    # A relative path is refused rather than quietly turning the scanner off.
    output_scanner: Annotated[
        Path | None, BeforeValidator(_empty_is_none), AfterValidator(_absolute)
    ] = None
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
    mimo_reasoning: Literal["enabled", "disabled"] = "enabled"
    opencode_executable: str = "opencode"
    nvidia_api_key: SecretStr | None = None
    # JSON list of ExtraProvider entries; they come after the built-in providers
    # of their role, and `disabled_providers` can switch any of them off.
    extra_providers: Annotated[list[ExtraProvider], AfterValidator(_unique_names)] = (
        Field(default_factory=list[ExtraProvider])
    )
