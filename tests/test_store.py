import sqlite3
from datetime import date

import pytest

from todd import db, store
from todd.errors import ToddError
from todd.models import EntryKind, Kind, Link, LinkKind, Priority, Role, State, Task

from .conftest import SLACK_DM


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.sqlite")


def _task(**kw) -> Task:
    links = [
        Link(LinkKind.SLACK, SLACK_DM, ref="D024BE91L/1790776800.123456", quote="can you send it?"),
        Link(LinkKind.JIRA, None, ref="PLAT-412"),
    ]
    return Task(
        title="Send Priya the numbers", description="send priya the numbers", links=links, **kw
    )


def test_migrates_a_new_database(conn):
    assert db.current_version(conn) == 1
    assert db.migrate(conn) == 1  # idempotent


def test_add_and_get_round_trip(conn):
    task_id = store.add(conn, _task())
    task = store.get(conn, task_id)
    assert task.state == State.INBOX
    assert task.description == "send priya the numbers"
    assert [(link.position, link.kind, link.quote) for link in task.links] == [
        (1, LinkKind.SLACK, "can you send it?"),
        (2, LinkKind.JIRA, None),
    ]
    assert [e.text for e in task.entries] == ["Captured"]
    assert not task.triaged


def test_missing_task(conn):
    with pytest.raises(ToddError, match="There's no task #9"):
        store.get(conn, 9)


def test_update_fields(conn):
    task_id = store.add(conn, _task())
    store.update(
        conn,
        task_id,
        kind=Kind.REPLY,
        priority=Priority.HIGH,
        due=date(2026, 10, 1),
        area="platform",
    )
    store.set_people(conn, task_id, ["Priya", "priya", " Sam "])
    task = store.get(conn, task_id)
    assert (task.kind, task.priority, task.due, task.area) == (
        Kind.REPLY,
        Priority.HIGH,
        date(2026, 10, 1),
        "platform",
    )
    assert task.people == ["Priya", "Sam"]
    with pytest.raises(ValueError, match="not task fields"):
        store.update(conn, task_id, state="done")


def test_state_changes_are_logged_and_waiting_is_forgotten_on_leaving(conn):
    task_id = store.add(conn, _task())
    assert (
        store.set_state(conn, task_id, State.WAITING, waiting_on="Priya to confirm") == State.INBOX
    )
    assert store.get(conn, task_id).waiting_on == "Priya to confirm"
    store.set_state(conn, task_id, State.DONE, note="sent them over")
    task = store.get(conn, task_id)
    assert task.waiting_on is None
    assert [(e.kind, e.text) for e in task.entries][1:] == [
        (EntryKind.STATE, "inbox → waiting on Priya to confirm"),
        (EntryKind.STATE, "waiting → done"),
        (EntryKind.NOTE, "sent them over"),
    ]


def test_filters(conn):
    a = store.add(conn, _task())
    b = store.add(conn, Task(title="Review PR"))
    store.update(conn, a, area="platform", kind=Kind.REPLY)
    store.set_people(conn, a, ["Priya Nair"])
    store.update(conn, b, area="hiring", kind=Kind.REVIEW)
    store.set_state(conn, b, State.DONE)
    assert [t.id for t in store.tasks(conn)] == [a, b]
    assert [t.id for t in store.tasks(conn, [State.INBOX])] == [a]
    assert [t.id for t in store.tasks(conn, area="PLATFORM")] == [a]
    assert [t.id for t in store.tasks(conn, kind=Kind.REVIEW)] == [b]
    assert [t.id for t in store.tasks(conn, person="priya")] == [a]
    assert store.areas(conn) == ["hiring", "platform"]
    assert store.counts(conn) == {State.INBOX: 1, State.DONE: 1}


def test_open_tasks_with_the_same_link(conn):
    a = store.add(conn, _task())
    b = store.add(conn, _task())
    store.set_state(conn, b, State.DONE)
    assert store.open_tasks_with(conn, Link(LinkKind.JIRA, None, ref="PLAT-412")) == [a]
    assert store.open_tasks_with(conn, Link(LinkKind.URL, "https://nope.example")) == []


def test_links_appended_later_continue_numbering(conn):
    task_id = store.add(conn, _task())
    store.add_links(conn, task_id, [Link(LinkKind.URL, "https://example.com")])
    store.update_link(conn, store.get(conn, task_id).links[0].id, role=Role.RESPOND, note="her ask")  # ty: ignore[invalid-argument-type]
    task = store.get(conn, task_id)
    assert [link.position for link in task.links] == [1, 2, 3]
    assert task.links[0].role == Role.RESPOND


def test_a_transaction_rolls_back_on_error(conn):
    task_id = store.add(conn, _task())
    with pytest.raises(RuntimeError), db.tx(conn):
        store.update(conn, task_id, title="changed")
        raise RuntimeError
    assert store.get(conn, task_id).title == "Send Priya the numbers"


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.sqlite"
    conn = db.connect(path)
    conn.execute("INSERT INTO schema_version VALUES (99, 'x')")
    conn.close()
    with pytest.raises(ToddError, match="newer than this todd"):
        db.connect(path)


def test_reorder_links_puts_the_given_ones_first(conn):
    task_id = store.add(conn, _task())
    store.add_links(conn, task_id, [Link(LinkKind.URL, "https://example.com")])
    ids = [link.id for link in store.get(conn, task_id).links]
    with db.tx(conn):
        store.reorder_links(conn, task_id, [ids[2], ids[0]])  # ty: ignore[invalid-argument-type]
    assert [link.id for link in store.get(conn, task_id).links] == [ids[2], ids[0], ids[1]]
