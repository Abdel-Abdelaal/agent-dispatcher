"""The local run queue: SQLite, one run at a time, safe across restarts."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_dispatcher.intake import Issue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    lease_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, birth REAL NOT NULL,
    deadline TEXT NOT NULL, finished_at TEXT, outcome TEXT
);
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
CREATE TABLE IF NOT EXISTS repairs (
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    sha TEXT NOT NULL,
    outcome TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parked_providers (
    provider TEXT PRIMARY KEY,
    until TEXT NOT NULL
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


@dataclass(frozen=True)
class RunRecord:
    """One past or active run, as recorded."""

    id: int
    repo: str
    number: int
    team: str
    started_at: str
    finished_at: str | None
    outcome: str | None


@dataclass(frozen=True)
class CycleLease:
    """A controller child process identity and its persisted cycle deadline."""

    lease_id: str
    pid: int
    birth: float
    deadline: str


class RunActiveError(RuntimeError):
    """Another run hasn't finished; runs are strictly serial."""


class RunQueue:
    """Tasks waiting for a run, and the history of runs."""

    def __init__(self, path: Path | str) -> None:
        """Open or create the queue database."""
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
        if "lease_id" not in columns:
            self._db.execute("ALTER TABLE runs ADD COLUMN lease_id TEXT")

    def close(self) -> None:
        """Close the database."""
        self._db.close()

    def enqueue(self, issue: Issue, now: datetime) -> bool:
        """Queue an issue; return False if it was already waiting.

        A task whose last run has finished comes back only because the owner put
        `agent:ready` on it again (the controller always moves that label away when
        a run starts), so it is queued again for a fresh run.
        """
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, ?)",
            (issue.repo, issue.number, issue.title, now.isoformat()),
        )
        if cursor.rowcount == 1:
            return True
        reopened = self._db.execute(
            """
            UPDATE tasks SET queued_at = ?, title = ?
            WHERE repo = ? AND number = ?
            AND EXISTS (
                SELECT 1 FROM runs r
                WHERE r.repo = tasks.repo AND r.number = tasks.number
                AND r.started_at >= tasks.queued_at
                AND r.finished_at IS NOT NULL AND r.outcome != 'parked'
            )
            AND NOT EXISTS (
                SELECT 1 FROM runs r
                WHERE r.repo = tasks.repo AND r.number = tasks.number
                AND r.finished_at IS NULL
            )
            """,
            (now.isoformat(), issue.title, issue.repo, issue.number),
        )
        return reopened.rowcount == 1

    def next_task(self) -> Task | None:
        """Return the oldest task with no run yet, or whose runs were all parked."""
        tasks = self.waiting()
        return tasks[0] if tasks else None

    def start_run(
        self, task: Task, provider: str, now: datetime, *, lease_id: str | None = None
    ) -> int:
        """Record a run start, or raise if one is already active."""
        try:
            cursor = self._db.execute(
                "INSERT INTO runs (repo, number, provider, started_at, lease_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (task.repo, task.number, provider, now.isoformat(), lease_id),
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

    def park(self, provider: str, until: datetime) -> None:
        """Mark a provider exhausted until `until`."""
        self._db.execute(
            "INSERT OR REPLACE INTO parked_providers VALUES (?, ?)",
            (provider, until.isoformat()),
        )

    def parked_until(self) -> dict[str, datetime]:
        """Return each parked provider and when it may run again."""
        rows = self._db.execute("SELECT provider, until FROM parked_providers")
        return {name: datetime.fromisoformat(until) for name, until in rows}

    def repairs(self, repo: str, pr: int) -> int:
        """Return how many CI repairs a PR has had."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM repairs WHERE repo = ? AND pr = ?", (repo, pr)
        ).fetchone()
        return int(row[0])

    def record_repair(
        self, repo: str, pr: int, sha: str, outcome: str, now: datetime
    ) -> int:
        """Remember a repair attempt, so a PR is never repaired twice for one cap."""
        cursor = self._db.execute(
            "INSERT INTO repairs VALUES (?, ?, ?, ?, ?)",
            (repo, pr, sha, outcome, now.isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def finish_repair(self, attempt: int, outcome: str, now: datetime) -> None:
        """Finish a reserved attempt; interruption never refunds its allowance."""
        self._db.execute(
            "UPDATE repairs SET outcome=?,at=? WHERE rowid=?",
            (outcome, now.isoformat(), attempt),
        )

    def waiting(self) -> list[Task]:
        """Return tasks with no run since they were queued, apart from parked ones."""
        rows = self._db.execute(
            """
            SELECT t.repo, t.number, t.title FROM tasks t
            WHERE NOT EXISTS (
                SELECT 1 FROM runs r
                WHERE r.repo = t.repo AND r.number = t.number
                AND r.started_at >= t.queued_at
                AND (r.outcome IS NULL OR r.outcome != 'parked')
            )
            ORDER BY t.queued_at, t.repo, t.number
            """
        )
        return [Task(*row) for row in rows]

    def recent_runs(self, limit: int = 10) -> list[RunRecord]:
        """Return the most recent runs, newest first."""
        rows = self._db.execute(
            "SELECT id, repo, number, provider, started_at, finished_at, outcome"
            " FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [RunRecord(*row) for row in rows]

    def register_cycle(self, lease: CycleLease) -> None:
        """Record only a process started by the supervisor, never an arbitrary PID."""
        self._db.execute(
            "INSERT INTO cycles (lease_id,pid,birth,deadline) VALUES (?,?,?,?)",
            (lease.lease_id, lease.pid, lease.birth, lease.deadline),
        )

    def active_cycles(self) -> list[CycleLease]:
        """Return unfinished leases, including those surviving a restart."""
        return [
            CycleLease(*row)
            for row in self._db.execute(
                "SELECT lease_id,pid,birth,deadline FROM cycles "
                "WHERE finished_at IS NULL"
            )
        ]

    def cycle_has_active_run(self, lease_id: str) -> bool:
        """Return whether this exact child left an unfinished run."""
        return (
            self._db.execute(
                "SELECT 1 FROM runs WHERE lease_id=? AND finished_at IS NULL",
                (lease_id,),
            ).fetchone()
            is not None
        )

    def settle_cycle(
        self, lease_id: str, outcome: str, now: datetime
    ) -> list[RunRecord]:
        """Finish exact-lease orphans, preserving all history and repair allowances."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            records = [
                RunRecord(*row)
                for row in self._db.execute(
                    "SELECT id,repo,number,provider,started_at,finished_at,outcome "
                    "FROM runs WHERE lease_id=? AND finished_at IS NULL",
                    (lease_id,),
                )
            ]
            self._db.execute(
                "UPDATE runs SET finished_at=?,outcome=? WHERE lease_id=? "
                "AND finished_at IS NULL",
                (now.isoformat(), outcome, lease_id),
            )
            self._db.execute(
                "UPDATE cycles SET finished_at=?,outcome=? WHERE lease_id=? "
                "AND finished_at IS NULL",
                (now.isoformat(), outcome, lease_id),
            )
            self._db.execute("COMMIT")
        except sqlite3.Error:
            self._db.execute("ROLLBACK")
            raise
        return records
