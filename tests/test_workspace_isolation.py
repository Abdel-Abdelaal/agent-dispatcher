"""Real Git regressions for the boundary between an agent clone and its controller."""

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_dispatcher.providers import scrubbed_env, subprocess_runner
from agent_dispatcher.workspace import GitError, Workspace


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=directory,
        env=scrubbed_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Workspace, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    (origin / "sample.txt").write_text("baseline\n", encoding="utf-8")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "baseline")
    root = tmp_path / "runs"
    root.mkdir()
    workspace = Workspace(root, subprocess_runner, scrubbed_env(os.environ))
    return workspace, workspace.clone(str(origin), "candidate")


def test_agent_git_filter_never_executes_during_patch_extraction(
    candidate: tuple[Workspace, Path], tmp_path: Path
) -> None:
    workspace, clone = candidate
    marker = tmp_path / "filter-executed"
    script = clone / "filter.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        + f"Path({str(marker)!r}).write_text('executed')\n"
        + "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    command = (
        shlex.quote(Path(sys.executable).as_posix())
        + " "
        + shlex.quote(script.as_posix())
    )
    git(clone, "config", "filter.agent.clean", command)
    (clone / ".gitattributes").write_text("sample.txt filter=agent\n", encoding="utf-8")
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    patch = workspace.patch(clone)
    assert not marker.exists()
    assert "+changed" in patch
    assert "-baseline" in patch


def test_agent_cannot_replace_the_controller_diff_base(
    candidate: tuple[Workspace, Path],
) -> None:
    workspace, clone = candidate
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "agent hides its change")
    patch = workspace.patch(clone)
    assert "+changed" in patch
    assert "-baseline" in patch


def test_unregistered_clone_is_rejected_before_git_runs(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, subprocess_runner, {})
    with pytest.raises(GitError, match="controller-owned"):
        workspace.patch(tmp_path)


def test_trusted_attributes_disable_a_configured_filter(
    candidate: tuple[Workspace, Path],
    tmp_path: Path,
) -> None:
    workspace, clone = candidate
    marker = tmp_path / "trusted-filter-executed"
    script = tmp_path / "configured-filter.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        + f"Path({str(marker)!r}).write_text('executed')\n"
        + "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    metadata = clone.parent / "controller-git" / clone.name
    command = (
        shlex.quote(Path(sys.executable).as_posix())
        + " "
        + shlex.quote(script.as_posix())
    )
    git(tmp_path, "--git-dir", str(metadata), "config", "filter.agent.clean", command)
    (clone / ".gitattributes").write_text("sample.txt filter=agent\n", encoding="utf-8")
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    assert "+changed" in workspace.patch(clone)
    assert not marker.exists()


def test_patch_preserves_new_files_and_deletions(
    candidate: tuple[Workspace, Path],
) -> None:
    workspace, clone = candidate
    (clone / "sample.txt").unlink()
    (clone / "new.txt").write_text("new content\n", encoding="utf-8")
    patch = workspace.patch(clone)
    assert "deleted file" in patch
    assert "new file" in patch
    assert "+new content" in patch


def test_agent_config_and_index_corruption_do_not_control_extraction(
    candidate: tuple[Workspace, Path],
) -> None:
    workspace, clone = candidate
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    (clone / ".git/config").write_text("[broken", encoding="utf-8")
    (clone / ".git/index").write_bytes(b"not an index")
    assert "+changed" in workspace.patch(clone)


def test_patch_git_never_receives_controller_credentials(
    candidate: tuple[Workspace, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, clone = candidate
    environments: list[dict[str, str]] = []

    def record(
        argv: list[str], cwd: Path, stdin: str, env: Mapping[str, str]
    ) -> tuple[int, str, str]:
        environments.append(dict(env))
        return subprocess_runner(argv, cwd, stdin, env)

    monkeypatch.setattr(workspace, "_runner", record)
    monkeypatch.setattr(
        workspace,
        "_env",
        {
            **scrubbed_env(os.environ),
            "GH_TOKEN": "dummy",
            "MIMO_API_KEY": "dummy",
        },
    )
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    assert "+changed" in workspace.patch(clone)
    assert len(environments) == 2
    assert all(
        "GH_TOKEN" not in env and "MIMO_API_KEY" not in env for env in environments
    )
    assert all(env["GIT_CONFIG_NOSYSTEM"] == "1" for env in environments)


def test_clone_disables_filters_before_checkout(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    (origin / "sample.txt").write_text("baseline\n", encoding="utf-8")
    (origin / ".gitattributes").write_text(
        "sample.txt filter=agent\n", encoding="utf-8"
    )
    git(origin, "add", ".")
    git(origin, "commit", "-m", "baseline")
    marker = tmp_path / "smudge-executed"
    script = tmp_path / "smudge.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        + f"Path({str(marker)!r}).write_text('executed')\n"
        + "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".gitconfig"
    command = (
        shlex.quote(Path(sys.executable).as_posix())
        + " "
        + shlex.quote(script.as_posix())
    )
    git(tmp_path, "config", "--file", str(config), "filter.agent.smudge", command)
    root = tmp_path / "runs"
    root.mkdir()
    env = {
        **scrubbed_env(os.environ),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(config),
    }
    workspace = Workspace(root, subprocess_runner, env)
    clone = workspace.clone(str(origin), "candidate")
    assert not marker.exists()
    assert (clone / "sample.txt").read_text(encoding="utf-8") == "baseline\n"


def test_relative_work_root_keeps_control_metadata_outside_the_candidate(
    candidate: tuple[Workspace, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del candidate
    monkeypatch.chdir(tmp_path)
    root = Path("relative-runs")
    root.mkdir()
    workspace = Workspace(root, subprocess_runner, scrubbed_env(os.environ))
    clone = workspace.clone(str(tmp_path / "origin"), "candidate")
    assert clone.is_absolute()
    (clone / "sample.txt").write_text("changed\n", encoding="utf-8")
    assert "+changed" in workspace.patch(clone)
    assert (root / "controller-git/candidate/HEAD").exists()
