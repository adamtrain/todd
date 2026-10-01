"""All the SQL. Takes and returns model objects."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from todd.db import tx
from todd.errors import ToddError
from todd.models import (
    Entry,
    EntryKind,
    Followup,
    FollowupStatus,
    Link,
    LinkKind,
    Priority,
    Reviewer,
    ReviewState,
    Role,
    State,
    Task,
    TaskRef,
)

TASK_FIELDS = frozenset(
    {
        "title",
        "description",
        "next_action",
        "area",
        "priority",
        "due",
        "due_hint",
        "waiting_on",
        "triaged_at",
        "needs_title",
        "is_project",
        "project_id",
        "project_position",
    }
)
LINK_FIELDS = frozenset(
    {
        "quote",
        "author",
        "role",
        "note",
        "title",
        "status",
        "fetched_at",
        "url",
        "ref",
        "stack",
        "stack_position",
        "role_fixed",
    }
)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _sql(value: Any) -> Any:
    """Python values as SQLite wants them."""
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


# ── Reading ──────────────────────────────────────────────────────────────────


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        next_action=row["next_action"],
        area=row["area"],
        is_project=bool(row["is_project"]),
        project_id=row["project_id"],
        project_position=row["project_position"],
        priority=Priority(row["priority"]),
        due=date.fromisoformat(row["due"]) if row["due"] else None,
        due_hint=row["due_hint"],
        state=State(row["state"]),
        waiting_on=row["waiting_on"],
        needs_title=bool(row["needs_title"]),
        triaged_at=_moment(row["triaged_at"]),
        created_at=_moment(row["created_at"]),
        updated_at=_moment(row["updated_at"]),
        state_at=_moment(row["state_at"]),
    )


def _link(row: sqlite3.Row) -> Link:
    return Link(
        id=row["id"],
        position=row["position"],
        kind=LinkKind(row["kind"]),
        url=row["url"],
        ref=row["ref"],
        quote=row["quote"],
        author=row["author"],
        role=Role(row["role"]) if row["role"] else None,
        note=row["note"],
        title=row["title"],
        status=row["status"],
        fetched_at=_moment(row["fetched_at"]),
        stack=row["stack"],
        stack_position=row["stack_position"],
        role_fixed=bool(row["role_fixed"]),
    )


def _ref(row: sqlite3.Row) -> TaskRef:
    return TaskRef(row["id"], row["title"], State(row["state"]))


def _followup(row: sqlite3.Row) -> Followup:
    return Followup(
        id=row["id"],
        task_id=row["task_id"],
        action=row["action"],
        person=row["person"],
        due=date.fromisoformat(row["due"]) if row["due"] else None,
        when=State(row["on_state"]) if row["on_state"] else None,
        unless=State(row["unless_state"]) if row["unless_state"] else None,
        status=FollowupStatus(row["status"]),
        by_you=bool(row["by_you"]),
        created_at=_moment(row["created_at"]),
        closed_at=_moment(row["closed_at"]),
    )


def _attach(conn: sqlite3.Connection, tasks: list[Task], *, entries: bool = False) -> list[Task]:
    """Fill in links, people (and optionally entries) for a batch of tasks."""
    by_id = {t.id: t for t in tasks}
    if not by_id:
        return tasks
    marks = ",".join("?" * len(by_id))
    ids = list(by_id)
    links: dict[int, Link] = {}
    for row in conn.execute(
        f"SELECT * FROM link WHERE task_id IN ({marks}) ORDER BY task_id, position", ids
    ):
        link = _link(row)
        links[row["id"]] = link
        by_id[row["task_id"]].links.append(link)
    for row in conn.execute(
        "SELECT r.* FROM review r JOIN link l ON l.id = r.link_id "
        f"WHERE l.task_id IN ({marks}) ORDER BY r.rowid",
        ids,
    ):
        links[row["link_id"]].reviewers.append(
            Reviewer(
                row["reviewer"], ReviewState(row["state"]), bool(row["team"]), bool(row["you"])
            )
        )
    for row in conn.execute(f"SELECT * FROM followup WHERE task_id IN ({marks}) ORDER BY id", ids):
        by_id[row["task_id"]].followups.append(_followup(row))
    for row in conn.execute(
        "SELECT b.task_id, t.id, t.title, t.state FROM blocker b "
        "JOIN task t ON t.id = b.blocked_by "
        f"WHERE b.task_id IN ({marks}) ORDER BY t.project_position, t.id",
        ids,
    ):
        by_id[row["task_id"]].blockers.append(_ref(row))
    project_ids = {t.project_id for t in tasks if t.project_id is not None}
    if project_ids:
        pmarks = ",".join("?" * len(project_ids))
        refs = {
            row["id"]: _ref(row)
            for row in conn.execute(
                f"SELECT id, title, state FROM task WHERE id IN ({pmarks})", list(project_ids)
            )
        }
        for task in tasks:
            if task.project_id is not None:
                task.project = refs.get(task.project_id)
    for row in conn.execute(
        f"SELECT task_id, name FROM person WHERE task_id IN ({marks}) ORDER BY rowid", ids
    ):
        by_id[row["task_id"]].people.append(row["name"])
    if entries:
        for row in conn.execute(
            f"SELECT * FROM entry WHERE task_id IN ({marks}) ORDER BY at, id", ids
        ):
            by_id[row["task_id"]].entries.append(
                Entry(
                    id=row["id"],
                    kind=EntryKind(row["kind"]),
                    text=row["text"],
                    at=datetime.fromisoformat(row["at"]),
                )
            )
    return tasks


def get(conn: sqlite3.Connection, task_id: int) -> Task:
    row = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ToddError(
            f"There's no task #{task_id}.", hint="See what's open with [bold]todd ls[/]."
        )
    return _attach(conn, [_task(row)], entries=True)[0]


def tasks(
    conn: sqlite3.Connection,
    states: Iterable[State] | None = None,
    *,
    area: str | None = None,
    person: str | Sequence[str] | None = None,
    projects: bool | None = None,
    project_id: int | None = None,
) -> list[Task]:
    """Tasks, filtered. `projects` True gives only projects, False only tasks, None both."""
    where, params = [], []
    if states is not None:
        wanted = list(states)
        where.append(f"state IN ({','.join('?' * len(wanted))})")
        params += [s.value for s in wanted]
    if area:
        where.append("area = ? COLLATE NOCASE")
        params.append(area)
    if projects is not None:
        where.append("is_project = ?")
        params.append(int(projects))
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if person:
        names = [person] if isinstance(person, str) else list(person)
        either = []
        for name in names:
            either.append(
                "EXISTS (SELECT 1 FROM person p WHERE p.task_id = task.id AND p.name LIKE ?)"
                " OR EXISTS (SELECT 1 FROM link l WHERE l.task_id = task.id AND l.author LIKE ?)"
                " OR EXISTS (SELECT 1 FROM followup f WHERE f.task_id = task.id"
                " AND f.status = 'open' AND f.person LIKE ?)"
                " OR EXISTS (SELECT 1 FROM review r JOIN link l ON l.id = r.link_id"
                " WHERE l.task_id = task.id AND r.reviewer LIKE ?)"
                " OR waiting_on LIKE ?"
            )
            params += [f"%{name}%"] * 5
        where.append("(" + " OR ".join(either) + ")")
    sql = "SELECT * FROM task"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = " ORDER BY project_position, id" if project_id is not None else " ORDER BY id"
    rows = conn.execute(sql + order, params).fetchall()
    return _attach(conn, [_task(r) for r in rows])


def areas(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT area, count(*) AS n FROM task WHERE area IS NOT NULL "
        "GROUP BY area COLLATE NOCASE ORDER BY n DESC, area"
    )
    return [r["area"] for r in rows]


def project_tasks(conn: sqlite3.Connection, project_id: int) -> list[Task]:
    """A project's tasks, in order."""
    return tasks(conn, project_id=project_id)


def dependents(conn: sqlite3.Connection, task_id: int) -> list[Task]:
    """Tasks waiting on this one."""
    rows = conn.execute(
        "SELECT t.* FROM task t JOIN blocker b ON b.task_id = t.id WHERE b.blocked_by = ? "
        "ORDER BY t.project_position, t.id",
        (task_id,),
    ).fetchall()
    return _attach(conn, [_task(r) for r in rows])


def open_followups(conn: sqlite3.Connection) -> list[Followup]:
    rows = conn.execute(
        "SELECT * FROM followup WHERE status = 'open' ORDER BY due IS NULL, due, id"
    )
    return [_followup(r) for r in rows]


def get_followup(conn: sqlite3.Connection, followup_id: int) -> Followup:
    row = conn.execute("SELECT * FROM followup WHERE id = ?", (followup_id,)).fetchone()
    if row is None:
        raise ToddError(
            f"There's no follow-up {followup_id}.", hint="See them with [bold]todd followup[/]."
        )
    return _followup(row)


def open_tasks_with(conn: sqlite3.Connection, link: Link) -> list[int]:
    """Open tasks that already carry this link (by ref when it has one, else by URL)."""
    column, value = ("ref", link.ref) if link.ref else ("url", link.url)
    rows = conn.execute(
        f"SELECT DISTINCT t.id FROM task t JOIN link l ON l.task_id = t.id "
        f"WHERE l.{column} = ? AND t.state NOT IN ('done','dropped') ORDER BY t.id",
        (value,),
    )
    return [r["id"] for r in rows]


def counts(conn: sqlite3.Connection) -> dict[State, int]:
    rows = conn.execute("SELECT state, count(*) AS n FROM task GROUP BY state")
    return {State(r["state"]): r["n"] for r in rows}


def closed_since(conn: sqlite3.Connection, since: datetime) -> int:
    row = conn.execute(
        "SELECT count(*) FROM task WHERE state = 'done' AND state_at >= ?", (stamp(since),)
    ).fetchone()
    return int(row[0])


# ── Writing ──────────────────────────────────────────────────────────────────


def insert_links(conn: sqlite3.Connection, task_id: int, links: Sequence[Link]) -> None:
    """Append links to a task (inside the caller's transaction)."""
    start = conn.execute(
        "SELECT coalesce(max(position), 0) FROM link WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    for i, link in enumerate(links, start + 1):
        cur = conn.execute(
            "INSERT INTO link (task_id, position, kind, url, ref, quote, author, role, note, "
            "title, status, fetched_at, stack, stack_position, role_fixed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                i,
                link.kind.value,
                link.url,
                link.ref,
                link.quote,
                link.author,
                link.role.value if link.role else None,
                link.note,
                link.title,
                link.status,
                _sql(link.fetched_at),
                link.stack,
                link.stack_position,
                link.role_fixed,
            ),
        )
        link.id, link.position = cur.lastrowid, i
        if link.reviewers:
            set_reviewers(conn, link.id, link.reviewers)  # ty: ignore[invalid-argument-type]


def set_reviewers(conn: sqlite3.Connection, link_id: int, reviewers: Sequence[Reviewer]) -> None:
    """Replace a pull request link's reviewers (inside the caller's transaction)."""
    conn.execute("DELETE FROM review WHERE link_id = ?", (link_id,))
    for r in reviewers:
        conn.execute(
            "INSERT OR REPLACE INTO review (link_id, reviewer, state, team, you) "
            "VALUES (?,?,?,?,?)",
            (link_id, r.login, r.state.value, r.team, r.you),
        )


def insert_followup(conn: sqlite3.Connection, task_id: int, followup: Followup) -> int:
    """Add a follow-up to a task (inside the caller's transaction)."""
    cur = conn.execute(
        "INSERT INTO followup (task_id, action, person, due, on_state, unless_state, by_you) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            task_id,
            followup.action,
            followup.person,
            _sql(followup.due),
            followup.when.value if followup.when else None,
            followup.unless.value if followup.unless else None,
            followup.by_you,
        ),
    )
    assert cur.lastrowid is not None
    followup.id, followup.task_id = cur.lastrowid, task_id
    return cur.lastrowid


def replace_claudes_followups(
    conn: sqlite3.Connection, task_id: int, followups: Sequence[Followup]
) -> None:
    """Swap the open follow-ups Claude suggested for new ones; yours and closed ones stay."""
    conn.execute(
        "DELETE FROM followup WHERE task_id = ? AND status = 'open' AND by_you = 0", (task_id,)
    )
    for followup in followups:
        insert_followup(conn, task_id, followup)


def close_followup(
    conn: sqlite3.Connection, followup_id: int, status: FollowupStatus, *, why: str | None = None
) -> Followup:
    """Mark a follow-up done or dropped, noting it on its task's timeline."""
    followup = get_followup(conn, followup_id)
    with tx(conn):
        conn.execute(
            "UPDATE followup SET status = ?, closed_at = ? WHERE id = ?",
            (status.value, stamp(now()), followup_id),
        )
        verb = "Done" if status == FollowupStatus.DONE else "Dropped"
        text = f"↪ {verb}: {followup.action}" + (f" ({why})" if why else "")
        assert followup.task_id is not None
        log(conn, followup.task_id, EntryKind.FOLLOWUP, text)
    followup.status = status
    return followup


def reschedule_followup(
    conn: sqlite3.Connection, followup_id: int, due: date | None, *, when: State | None = None
) -> None:
    """Give a follow-up a new date (and optionally a new state to fire on)."""
    get_followup(conn, followup_id)
    conn.execute(
        "UPDATE followup SET due = ?, on_state = ?, status = 'open', closed_at = NULL WHERE id = ?",
        (_sql(due), when.value if when else None, followup_id),
    )


def log(conn: sqlite3.Connection, task_id: int, kind: EntryKind, text: str) -> None:
    """Add to a task's timeline (inside the caller's transaction)."""
    conn.execute(
        "INSERT INTO entry (task_id, at, kind, text) VALUES (?,?,?,?)",
        (task_id, stamp(now()), kind.value, text),
    )


def move(
    conn: sqlite3.Connection, task_id: int, state: State, *, waiting_on: str | None = None
) -> None:
    """Change a task's state (inside the caller's transaction).

    Leaving `waiting` forgets what it was waiting on.
    """
    moment = stamp(now())
    conn.execute(
        "UPDATE task SET state = ?, state_at = ?, updated_at = ?, waiting_on = ? WHERE id = ?",
        (
            state.value,
            moment,
            moment,
            waiting_on if state == State.WAITING else None,
            task_id,
        ),
    )


# ── Numbers ──────────────────────────────────────────────────────────────────
#
# A task's number is its id, and numbers are kept as low as they can be: what's open is
# numbered 1, 2, 3… in the order it already had, and what's finished or dropped comes after,
# most recently closed first. So closing something frees its number for the ones after it,
# and the thing you just closed is the first number after your open ones. Follow-ups are
# numbered the same way.

# Every column that holds a task's number.
_TASK_NUMBERS = (
    ("task", "id"),
    ("task", "project_id"),
    ("blocker", "task_id"),
    ("blocker", "blocked_by"),
    ("link", "task_id"),
    ("person", "task_id"),
    ("entry", "task_id"),
    ("followup", "task_id"),
)
_CLOSED = (State.DONE.value, State.DROPPED.value)


@dataclass(frozen=True, slots=True)
class Numbers:
    """The numbers in use: which exist, and which of those are open."""

    tasks: frozenset[int] = frozenset()
    open_tasks: frozenset[int] = frozenset()
    followups: frozenset[int] = frozenset()
    open_followups: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class Renumbered:
    """What `renumber` changed, old number to new, and the numbers as they are now."""

    tasks: dict[int, int] = field(default_factory=dict)
    followups: dict[int, int] = field(default_factory=dict)
    now: Numbers = Numbers()

    def task(self, number: int) -> int:
        """What a task's number is now."""
        return self.tasks.get(number, number)

    def followup(self, number: int) -> int:
        return self.followups.get(number, number)


def _seconds(text: str | None) -> float:
    moment = _moment(text)
    return moment.timestamp() if moment else 0.0


def _task_order(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    """Task numbers as they should be ordered: the open ones, then the closed ones.

    A project is closed when it was dropped, or when every one of its tasks is closed; it
    closed when the last of them did.
    """
    rows = conn.execute(
        "SELECT id, state, state_at, is_project, project_id FROM task ORDER BY id"
    ).fetchall()
    # Timestamps are to the second, so the timeline settles which of two came later.
    last_move = dict(
        conn.execute("SELECT task_id, max(id) FROM entry WHERE kind = 'state' GROUP BY task_id")
    )
    tasks_of: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        if row["project_id"] is not None:
            tasks_of.setdefault(row["project_id"], []).append(row)

    def closed_at(row: sqlite3.Row) -> tuple[float, int]:
        return _seconds(row["state_at"]), last_move.get(row["id"], 0)

    still_open: list[int] = []
    closed: list[tuple[float, int, int]] = []
    for row in rows:
        when: tuple[float, int] | None = closed_at(row) if row["state"] in _CLOSED else None
        tasks = tasks_of.get(row["id"], []) if row["is_project"] else []
        if tasks and all(t["state"] in _CLOSED for t in tasks):
            when = max([closed_at(t) for t in tasks] + ([when] if when else []))
        if when is None:
            still_open.append(row["id"])
        else:
            closed.append((-when[0], -when[1], row["id"]))
    return still_open, [task_id for *_, task_id in sorted(closed)]


def _followup_order(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    rows = conn.execute("SELECT id, status, closed_at FROM followup ORDER BY id").fetchall()
    still_open = [r["id"] for r in rows if r["status"] == FollowupStatus.OPEN.value]
    closed = sorted(
        (-_seconds(r["closed_at"]), r["id"])
        for r in rows
        if r["status"] != FollowupStatus.OPEN.value
    )
    return still_open, [followup_id for _, followup_id in closed]


def numbers(conn: sqlite3.Connection) -> Numbers:
    tasks, closed_tasks = _task_order(conn)
    followups, closed_followups = _followup_order(conn)
    return Numbers(
        frozenset(tasks) | frozenset(closed_tasks),
        frozenset(tasks),
        frozenset(followups) | frozenset(closed_followups),
        frozenset(followups),
    )


def _renumber(
    conn: sqlite3.Connection, order: list[int], columns: Sequence[tuple[str, str]]
) -> dict[int, int]:
    """Number the things in `order` 1, 2, 3…, everywhere `columns` hold their numbers (inside
    the caller's transaction). Returns the ones that changed, old number to new."""
    changes = {old: new for new, old in enumerate(order, 1) if old != new}
    if not changes:
        return {}
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS renumbering (old INTEGER PRIMARY KEY, new INTEGER)"
    )
    conn.execute("DELETE FROM renumbering")
    conn.executemany("INSERT INTO renumbering (old, new) VALUES (?, ?)", changes.items())
    # Through negative numbers first, so that no two rows ever share one on the way.
    for table, column in columns:
        conn.execute(
            f"UPDATE {table} SET {column} = "
            f"-(SELECT new FROM renumbering WHERE old = {table}.{column}) "
            f"WHERE {column} IN (SELECT old FROM renumbering)"
        )
    for table, column in columns:
        conn.execute(f"UPDATE {table} SET {column} = -{column} WHERE {column} < 0")
    return changes


def renumber(conn: sqlite3.Connection) -> Renumbered:
    """Give tasks and follow-ups the lowest numbers there are: open ones first, in the order
    they have, then closed ones, most recently closed first."""
    tasks, closed_tasks = _task_order(conn)
    followups, closed_followups = _followup_order(conn)
    with tx(conn):
        # Rows point at each other by number; they only need to agree again by the end.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        moved = _renumber(conn, tasks + closed_tasks, _TASK_NUMBERS)
        moved_followups = _renumber(conn, followups + closed_followups, (("followup", "id"),))
    return Renumbered(moved, moved_followups, numbers(conn))


def insert_task(conn: sqlite3.Connection, task: Task) -> int:
    """Save a new task's own fields and links (inside the caller's transaction)."""
    cur = conn.execute(
        "INSERT INTO task (title, description, state, is_project, project_id, project_position) "
        "VALUES (?,?,?,?,?,?)",
        (
            task.title,
            task.description,
            task.state.value,
            task.is_project,
            task.project_id,
            task.project_position,
        ),
    )
    assert cur.lastrowid is not None
    task.id = cur.lastrowid
    insert_links(conn, task.id, task.links)
    return task.id


def add(conn: sqlite3.Connection, task: Task) -> int:
    """Save a newly captured task with its links. Returns its id."""
    with tx(conn):
        insert_task(conn, task)
        assert task.id is not None
        log(conn, task.id, EntryKind.NOTE, "Captured")
    return task.id


def next_position(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT coalesce(max(project_position), 0) FROM task WHERE project_id = ?", (project_id,)
    ).fetchone()
    return int(row[0]) + 1


def waits_on(conn: sqlite3.Connection, task_id: int, other: int) -> bool:
    """Whether `task_id` already waits on `other`, directly or through other tasks."""
    seen, frontier = set(), [task_id]
    while frontier:
        current = frontier.pop()
        if current == other:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier += [
            r["blocked_by"]
            for r in conn.execute("SELECT blocked_by FROM blocker WHERE task_id = ?", (current,))
        ]
    return False


def add_blocker(conn: sqlite3.Connection, task_id: int, blocked_by: int) -> None:
    """`task_id` can't start until `blocked_by` is done (inside the caller's transaction)."""
    if task_id == blocked_by:
        raise ToddError(f"#{task_id} can't wait on itself.")
    if waits_on(conn, blocked_by, task_id):
        raise ToddError(
            f"#{blocked_by} already waits on #{task_id}, so #{task_id} can't wait on it too."
        )
    conn.execute(
        "INSERT OR IGNORE INTO blocker (task_id, blocked_by) VALUES (?,?)", (task_id, blocked_by)
    )


def remove_blockers(conn: sqlite3.Connection, task_id: int, blocked_by: int | None = None) -> int:
    """Stop `task_id` waiting on `blocked_by` (or on anything). Returns how many were removed."""
    if blocked_by is None:
        cur = conn.execute("DELETE FROM blocker WHERE task_id = ?", (task_id,))
    else:
        cur = conn.execute(
            "DELETE FROM blocker WHERE task_id = ? AND blocked_by = ?", (task_id, blocked_by)
        )
    return cur.rowcount


def move_links(conn: sqlite3.Connection, link_ids: Sequence[int], task_id: int) -> None:
    """Hand links (and their reviewers) to another task, after any it already has.
    Inside the caller's transaction."""
    start = conn.execute(
        "SELECT coalesce(max(position), 0) FROM link WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    # Negative positions first, so UNIQUE (task_id, position) never sees a clash mid-way.
    for i, link_id in enumerate(link_ids, start + 1):
        conn.execute(
            "UPDATE link SET task_id = ?, position = ? WHERE id = ?", (task_id, -i, link_id)
        )
    conn.execute(
        "UPDATE link SET position = -position WHERE task_id = ? AND position < 0", (task_id,)
    )


def update(conn: sqlite3.Connection, task_id: int, **fields: Any) -> None:
    unknown = set(fields) - TASK_FIELDS
    if unknown:
        raise ValueError(f"not task fields: {sorted(unknown)}")
    if not fields:
        return
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE task SET {sets}, updated_at = ? WHERE id = ?",
        [_sql(v.value if isinstance(v, State | Priority) else v) for v in fields.values()]
        + [stamp(now()), task_id],
    )


def update_link(conn: sqlite3.Connection, link_id: int, **fields: Any) -> None:
    unknown = set(fields) - LINK_FIELDS
    if unknown:
        raise ValueError(f"not link fields: {sorted(unknown)}")
    if not fields:
        return
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE link SET {sets} WHERE id = ?",
        [_sql(v.value if isinstance(v, Role) else v) for v in fields.values()] + [link_id],
    )


def set_people(conn: sqlite3.Connection, task_id: int, names: Iterable[str]) -> None:
    conn.execute("DELETE FROM person WHERE task_id = ?", (task_id,))
    for name in names:
        if name.strip():
            conn.execute(
                "INSERT OR IGNORE INTO person (task_id, name) VALUES (?, ?)",
                (task_id, name.strip()),
            )


def reorder_links(conn: sqlite3.Connection, task_id: int, first: Sequence[int]) -> None:
    """Renumber a task's links: `first` (link ids) in that order, then the rest as they were.
    Inside the caller's transaction."""
    rows = conn.execute(
        "SELECT id FROM link WHERE task_id = ? ORDER BY position", (task_id,)
    ).fetchall()
    order = [i for i in first if i in {r["id"] for r in rows}]
    order += [r["id"] for r in rows if r["id"] not in order]
    # Two passes, so the UNIQUE (task_id, position) constraint never sees a clash.
    conn.execute("UPDATE link SET position = -position WHERE task_id = ?", (task_id,))
    for position, link_id in enumerate(order, 1):
        conn.execute("UPDATE link SET position = ? WHERE id = ?", (position, link_id))


def add_links(conn: sqlite3.Connection, task_id: int, links: Sequence[Link]) -> None:
    with tx(conn):
        insert_links(conn, task_id, links)
        conn.execute("UPDATE task SET updated_at = ? WHERE id = ?", (stamp(now()), task_id))


def note(conn: sqlite3.Connection, task_id: int, text: str, kind: EntryKind = EntryKind.NOTE):
    with tx(conn):
        log(conn, task_id, kind, text)
        conn.execute("UPDATE task SET updated_at = ? WHERE id = ?", (stamp(now()), task_id))


def set_state(
    conn: sqlite3.Connection,
    task_id: int,
    state: State,
    *,
    note: str | None = None,
    waiting_on: str | None = None,
) -> State:
    """Move a task to a new state, logging it. Returns the state it left."""
    task = get(conn, task_id)
    with tx(conn):
        move(conn, task_id, state, waiting_on=waiting_on or task.waiting_on)
        change = f"{task.state.label} → {state.label}"
        if state == State.WAITING and waiting_on:
            change += f" on {waiting_on}"
        log(conn, task_id, EntryKind.STATE, change)
        if note:
            log(conn, task_id, EntryKind.NOTE, note)
    return task.state


def add_followup(conn: sqlite3.Connection, task_id: int, followup: Followup) -> int:
    get(conn, task_id)
    with tx(conn):
        followup_id = insert_followup(conn, task_id, followup)
        log(conn, task_id, EntryKind.FOLLOWUP, f"↪ Added: {followup.action}")
    return followup_id
