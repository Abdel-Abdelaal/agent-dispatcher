"""What the controller pastes into prompts, so models don't spend steps finding it."""

import os
from pathlib import Path

import pytest

from agent_dispatcher.execute import changed_files, repo_rules, step_budget


def patch_for(*names: str) -> str:
    return "".join(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n"
        for name in names
    )


def test_agents_md_is_pasted_as_data(tmp_path: Path) -> None:
    assert repo_rules(tmp_path) == "This repository has no AGENTS.md.\n"
    (tmp_path / "AGENTS.md").write_text("# Rules\nRun pytest.\n", encoding="utf-8")
    assert (
        repo_rules(tmp_path)
        == "<<<AGENTS.md>>>\n# Rules\nRun pytest.\n<<<END_AGENTS.md>>>\n"
    )


def test_a_huge_agents_md_is_cut_with_a_pointer(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("a" * 20_000, encoding="utf-8")
    rules = repo_rules(tmp_path)
    assert len(rules) < 12_200
    assert "[truncated: open AGENTS.md for the rest]" in rules


def test_changed_files_are_pasted_after_the_change(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('new')\n", encoding="utf-8")
    text = changed_files(tmp_path, patch_for("src/a.py", "src/a.py", "gone.py"))
    # Each file once, and a deleted file (no new contents) is left out.
    assert text == "<<<FILE src/a.py>>>\nprint('new')\n<<<END_FILE src/a.py>>>\n"


def test_large_files_are_named_but_not_pasted(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("b" * 31_000, encoding="utf-8")
    for index in range(3):
        (tmp_path / f"part{index}.txt").write_text("p" * 25_000, encoding="utf-8")
    text = changed_files(
        tmp_path, patch_for("big.txt", "part0.txt", "part1.txt", "part2.txt")
    )
    assert "big.txt: too large to include here; open it if needed." in text
    assert "<<<FILE part0.txt>>>" in text
    assert "<<<FILE part1.txt>>>" in text
    # The third would pass the 60 000 character total.
    assert "part2.txt: too large to include here; open it if needed." in text


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_and_paths_outside_the_clone_are_never_read(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (clone / "link.txt").symlink_to(secret)
    (clone / "AGENTS.md").symlink_to(secret)
    text = changed_files(clone, patch_for("link.txt", "../secret.txt"))
    assert "outside" not in text
    assert "outside" not in repo_rules(clone)


def test_paths_outside_the_clone_are_never_read(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    assert changed_files(clone, patch_for("../secret.txt")) == ""


def test_the_step_budget_names_the_cap() -> None:
    assert "at most 10 tool steps" in step_budget(10)
