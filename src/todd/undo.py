"""`todd undo`: put todd back the way it was before the last command that changed something.

When a command opens the database, todd copies it into memory first. If the command changes
anything, that copy is kept beside the database, with what was run, and `todd undo` restores
the newest one. Only todd's own records go back: a Jira ticket it moved or a Slack reply you
sent stay as they are.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from todd import db
from todd.errors import ToddError

KEEP = 20  # how many changes back you can go


@dataclass(frozen=True, slots=True)
class Undone:
    command: str  # what was run, as typed
    at: datetime
    outside: list[str]  # things it did outside todd, which undoing leaves alone


def directory(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".undo")


class Before:
    """The database as a command found it, kept until the command ends."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.copy = sqlite3.connect(":memory:")
        conn.backup(self.copy)
        self.changes = conn.total_changes

    def keep(self, conn: sqlite3.Connection, db_path: Path, command: str) -> None:
        """If the command changed anything, keep the copy so `todd undo` can go back to it."""
        try:
            if conn.total_changes == self.changes:
                return
            folder = directory(db_path)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            with closing(sqlite3.connect(folder / f"{stamp}.sqlite")) as saved:
                self.copy.backup(saved)
            (folder / f"{stamp}.json").write_text(json.dumps({"command": command}))
            for old in _snapshots(folder)[:-KEEP]:
                _remove(old)
        finally:
            self.copy.close()


def _snapshots(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.sqlite")) if folder.is_dir() else []


def _remove(snapshot: Path) -> None:
    with suppress(OSError):
        snapshot.unlink()
    with suppress(OSError):
        snapshot.with_suffix(".json").unlink()


def _outside(conn: sqlite3.Connection, since: int) -> list[str]:
    """Jira and Slack entries written after the snapshot: things todd did beyond itself."""
    rows = conn.execute(
        "SELECT e.text, e.task_id FROM entry e WHERE e.id > ? AND e.kind IN ('jira','slack') "
        "ORDER BY e.id",
        (since,),
    ).fetchall()
    return [f"#{row['task_id']}: {row['text']}" for row in rows]


def undo(conn: sqlite3.Connection, db_path: Path) -> Undone:
    """Restore the newest snapshot over the database, and forget it."""
    snapshots = _snapshots(directory(db_path))
    if not snapshots:
        raise ToddError("There's nothing to undo.")
    snapshot = snapshots[-1]
    try:
        meta = json.loads(snapshot.with_suffix(".json").read_text())
    except (OSError, ValueError):
        meta = {}
    at = datetime.strptime(snapshot.stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    try:
        with closing(sqlite3.connect(snapshot)) as saved:
            last_entry = saved.execute("SELECT coalesce(max(id), 0) FROM entry").fetchone()[0]
            outside = _outside(conn, last_entry)
            saved.backup(conn)
        db.migrate(conn)  # a snapshot from an older todd
    except sqlite3.Error as e:
        raise ToddError("Couldn't undo.", detail=str(e)) from e
    _remove(snapshot)
    return Undone(meta.get("command") or "a change", at, outside)
