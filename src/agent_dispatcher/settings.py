"""Configuration, read once from `DISPATCHER_` environment variables."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the controller can be told; secrets stay `SecretStr`."""

    model_config = SettingsConfigDict(env_prefix="DISPATCHER_", env_file=".env")

    owner: str = "Abdel-Abdelaal"
    repos: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Abdel-Abdelaal/shortlink"]
    )
    db_path: Path = Path("dispatcher.sqlite3")
    max_weekly_share: float = Field(default=0.6, gt=0, le=1)
    max_files: int = Field(default=15, gt=0)
    max_lines: int = Field(default=400, gt=0)
    github_token: SecretStr | None = None
    mimo_api_key: SecretStr | None = None
    nvidia_api_key: SecretStr | None = None

    @field_validator("repos", mode="before")
    @classmethod
    def _split_repos(cls, value: object) -> object:
        """Accept `owner/a,owner/b` from the environment."""
        if isinstance(value, str):
            return [repo.strip() for repo in value.split(",") if repo.strip()]
        return value
