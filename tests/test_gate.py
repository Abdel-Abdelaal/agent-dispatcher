import pytest

from agent_dispatcher.gate import (
    FileChange,
    Limits,
    MalformedDiffError,
    is_protected,
    parse_numstat,
    refusals,
)

LIMITS = Limits(max_files=3, max_lines=50)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".GitHub\\workflows\\ci.yml",
        ".claude/settings.json",
        "pkg/.mcp.json",
        "docs/AGENTS.md",
        "CLAUDE.md",
        ".github/CODEOWNERS",
        "uv.lock",
        "web/package-lock.json",
        ".env.production",
        "gradle/wrapper/gradle-wrapper.properties",
    ],
)
def test_protected_paths_at_any_depth_and_case(path: str) -> None:
    assert is_protected(path)


@pytest.mark.parametrize(
    "path", ["src/app/main.py", "tests/test_main.py", "docs/github.md", "README.md"]
)
def test_ordinary_paths_are_not_protected(path: str) -> None:
    assert not is_protected(path)


def test_numstat_parses_text_and_binary() -> None:
    changes = parse_numstat("3\t1\tsrc/a.py\n\n-\t-\tlogo.png\n")
    assert changes == [FileChange("src/a.py", 3, 1), FileChange("logo.png", None, None)]


def test_numstat_fails_closed() -> None:
    with pytest.raises(MalformedDiffError):
        parse_numstat("not a numstat line")


def test_small_ordinary_diff_passes() -> None:
    assert refusals([FileChange("src/a.py", 10, 2)], LIMITS) == []


def test_every_problem_is_reported() -> None:
    changes = [
        FileChange(".claude/settings.json", 1, 0),
        FileChange("../outside.py", 1, 0),
        FileChange("logo.png", None, None),
        FileChange("tests/test_a.py", 0, 30),
    ]
    reasons = refusals(changes, LIMITS)
    assert reasons == [
        "protected path: .claude/settings.json",
        "path escapes the repository: ../outside.py",
        "binary file: logo.png",
        "test lines deleted without additions: tests/test_a.py",
        "4 files changed, limit 3",
    ]


def test_line_cap() -> None:
    reasons = refusals([FileChange("src/a.py", 40, 20)], LIMITS)
    assert reasons == ["60 lines changed, limit 50"]


def test_test_deletion_can_be_allowed_per_task() -> None:
    limits = Limits(max_files=3, max_lines=50, allow_test_deletions=True)
    assert refusals([FileChange("tests/test_a.py", 0, 5)], limits) == []
