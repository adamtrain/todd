"""Where todd keeps its files, and opening (and migrating) the database."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from todd.errors import ToddError
from todd.migrations import LATEST_VERSION, MIGRATIONS

DB_ENV = "TODD_DB"
CONFIG_ENV = "TODD_CONFIG"


def home() -> Path:
    """~/.config/todd, honouring $XDG_CONFIG_HOME when it is set."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "todd"


def db_path(cli_arg: str | Path | None = None) -> Path:
    """--db, then $TODD_DB, then ~/.config/todd/todd.sqlite."""
    if cli_arg:
        return Path(cli_arg).expanduser()
    if env := os.environ.get(DB_ENV):
        return Path(env).expanduser()
    return home() / "todd.sqlite"


def config_path(cli_arg: str | Path | None = None) -> Path:
    """--config, then $TODD_CONFIG, then ~/.config/todd/config.toml."""
    if cli_arg:
        return Path(cli_arg).expanduser()
    if env := os.environ.get(CONFIG_ENV):
        return Path(env).expanduser()
    return home() / "config.toml"


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    current = current_version(conn)
    if current > LATEST_VERSION:
        raise ToddError(
            f"This database uses schema version {current}, newer than this todd understands "
            f"({LATEST_VERSION}).",
            hint="Upgrade todd.",
        )
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        script = (
            "BEGIN;\n"
            + sql
            + f"\nINSERT INTO schema_version(version, applied_at) VALUES ({int(version)}, "
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'));\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating and migrating it as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN in tx()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 3000")
        migrate(conn)
    except sqlite3.Error as e:
        raise ToddError(f"Couldn't open {path}.", detail=str(e)) from e
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
