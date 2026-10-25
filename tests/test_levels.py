from agent_dispatcher.levels import (
    DEFAULT_LADDER,
    Level,
    harder,
    parse_level,
    split_rating,
)


def test_parse_level_accepts_only_the_three_names() -> None:
    assert parse_level(" Hard ") is Level.HARD
    assert parse_level("extreme") is None
    assert parse_level(3) is None


def test_split_rating_reads_and_removes_the_first_line() -> None:
    assert split_rating("DIFFICULTY: easy\n1. step") == (Level.EASY, "1. step")
    assert split_rating("  difficulty: HARD  \n\n1. a") == (Level.HARD, "1. a")
    assert split_rating("1. no rating") == (None, "1. no rating")


def test_harder_stops_at_hard() -> None:
    assert harder(Level.EASY) is Level.MEDIUM
    assert harder(Level.HARD) is Level.HARD


def test_opus_only_for_hard_and_nothing_above_high() -> None:
    claude = DEFAULT_LADDER["claude"]
    assert [claude[level][0] for level in ("easy", "medium")] == [
        "claude-sonnet-5-5"
    ] * 2
    assert claude["hard"] == ("claude-opus-5-5", "high")
    assert DEFAULT_LADDER["codex"]["hard"] == ("gpt-6-astra", "high")
    efforts = {
        effort for steps in DEFAULT_LADDER.values() for _, effort in steps.values()
    }
    assert efforts <= {"low", "medium", "high"}
