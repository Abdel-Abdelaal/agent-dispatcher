"""Configuration, read once from `DISPATCHER_` environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the controller can be told; secrets stay `SecretStr`."""

    model_config = SettingsConfigDict(env_prefix="DISPATCHER_", env_file=".env")

    owner: str = "Abdel-Abdelaal"
    repos: list[str] = Field(default_factory=lambda: ["Abdel-Abdelaal/shortlink"])
    db_path: Path = Path("dispatcher.sqlite3")
    max_weekly_share: float = Field(default=0.6, gt=0, le=1)
    max_files: int = Field(default=15, gt=0)
    max_lines: int = Field(default=400, gt=0)
    github_token: SecretStr | None = None
    mimo_api_key: SecretStr | None = None
    nvidia_api_key: SecretStr | None = None
