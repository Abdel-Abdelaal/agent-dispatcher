"""Configuration, read once from `DISPATCHER_` environment variables."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    db_path: Path = Path("dispatcher.sqlite3")
    max_weekly_share: float = Field(default=0.6, gt=0, le=1)
    max_files: int = Field(default=15, gt=0)
    max_lines: int = Field(default=400, gt=0)
    work_root: Path = Path("runs")
    park_hours: float = Field(default=5, gt=0)
    github_token: SecretStr | None = None
    claude_oauth_token: SecretStr | None = None
    # Thinking (plan, review) gets the strong model at high effort; quality pays
    # most there and the steps are short. Implementing, which only happens when
    # both workers are out, gets a lighter model to save the scarce allowance.
    claude_think_model: str = "claude-opus-5-5"
    claude_think_effort: ClaudeEffort = "high"
    claude_work_model: str = "claude-sonnet-5-5"
    claude_work_effort: ClaudeEffort = "medium"
    codex_think_model: str = "gpt-6.1-sol"
    codex_think_effort: CodexEffort = "high"
    codex_work_model: str = "gpt-6.1-sol"
    codex_work_effort: CodexEffort = "medium"
    mimo_api_key: SecretStr | None = None
    nvidia_api_key: SecretStr | None = None
