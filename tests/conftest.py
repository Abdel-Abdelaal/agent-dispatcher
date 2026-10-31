"""Keep every test away from the controller's real configuration."""

import os
from pathlib import Path

import pytest

from agent_dispatcher.settings import Settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide `DISPATCHER_` variables and `.env`, and run from an empty folder.

    Run in the production checkout, the suite would otherwise read its `.env`:
    real keys, `auto` repositories and the real OpenCode CLI, so a test could
    make a live model call.
    """
    for name in list(os.environ):
        if name.upper().startswith("DISPATCHER_"):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.chdir(tmp_path)
