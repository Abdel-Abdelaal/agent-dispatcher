"""The local run queue: SQLite, one run at a time, safe across restarts."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_dispatcher.intake import Issue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT
);
-- At most one unfinished run, enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs ((1))
    WHERE finished_at IS NULL;
"""


@dataclass(frozen=True)
class Task:
    """A queued issue."""

    repo: str
    number: int
    title: str


class RunActiveError(RuntimeError):
    """Another run hasn't finished; runs are strictly serial."""


class RunQueue:
    """Tasks waiting for a run, and the history of runs."""

    def __init__(self, path: Path | str) -> None:
        """Open or create the queue database."""
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the database."""
        self._db.close()

    def enqueue(self, issue: Issue, now: datetime) -> bool:
        """Add an issue once; return False if it was already queued."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?)",
            (issue.repo, issue.number, issue.title, now.isoformat()),
        )
        return cursor.rowcount == 1

    def next_task(self) -> Task | None:
        """Return the oldest task that has never had a run."""
        row = self._db.execute(
            """
            SELECT t.repo, t.number, t.title FROM tasks t
            WHERE NOT EXISTS (
                SELECT 1 FROM runs r WHERE r.repo = t.repo AND r.number = t.number
            )
            ORDER BY t.queued_at, t.repo, t.number LIMIT 1
            """
        ).fetchone()
        return Task(*row) if row else None

    def start_run(self, task: Task, provider: str, now: datetime) -> int:
        """Record a run start, or raise if one is already active."""
        try:
            cursor = self._db.execute(
                "INSERT INTO runs (repo, number, provider, started_at)"
                " VALUES (?, ?, ?, ?)",
                (task.repo, task.number, provider, now.isoformat()),
            )
        except sqlite3.IntegrityError as error:
            message = "another run is still active"
            raise RunActiveError(message) from error
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, outcome: str, now: datetime) -> None:
        """Record how a run ended, which releases the single-run lock."""
        self._db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ? WHERE id = ?",
            (now.isoformat(), outcome, run_id),
        )
