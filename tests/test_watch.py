"""`todd watch`: the `todd now` view kept on screen, redrawn when the database changes."""

import io
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from datetime import date, datetime

import pytest
from rich.console import Console
from rich.screen import Screen

from todd import cli, db, intent, render, store
from todd.models import State, Task

from .conftest import TODAY
from .test_cli import todd

NOON = datetime(2026, 9, 30, 12, 0, 5).astimezone()


def elsewhere():
    """A connection of its own, as another terminal would have."""
    return db.connect(db.db_path())


def add(title: str, **fields) -> int:
    conn = elsewhere()
    task_id = store.add(conn, Task(title))
    store.set_state(conn, task_id, State.TODO)
    if fields:
        store.update(conn, task_id, **fields)
    return task_id


@pytest.fixture
def clock(monkeypatch):
    """The time `todd watch` thinks it is, which a test can move."""
    now = {"time": NOON}
    monkeypatch.setattr(cli, "_clock", lambda: now["time"])
    return now


def watching(monkeypatch, *between):
    """Run `todd watch` for one look, then one more after each of `between` has been done."""

    def ticks(every: float):
        yield
        for step in between:
            step()
            yield

    monkeypatch.setattr(cli, "_ticks", ticks)
    result = todd("watch")
    assert result.exit_code == 0, result.output
    return result.output.split("todd watch")[1:]  # one piece per view drawn


def test_data_version_notices_another_connections_changes(tmp_path):
    ours, theirs = db.connect(tmp_path / "t.sqlite"), db.connect(tmp_path / "t.sqlite")
    before = store.data_version(ours)
    assert store.data_version(ours) == before  # nothing happened
    store.add(theirs, Task("x"))
    assert store.data_version(ours) != before


def test_it_redraws_when_another_terminal_changes_something(shell, monkeypatch, clock):
    add("Reply to Priya")
    frames = watching(
        monkeypatch,
        lambda: add("Book the offsite"),
        lambda: None,  # nothing changed: nothing is redrawn
        lambda: store.set_state(elsewhere(), 1, State.DONE),
    )
    assert len(frames) == 3
    first, second, third = frames
    assert "updated 12:00:05 · Ctrl-C to stop" in first
    assert "Reply to Priya" in first and "Book the offsite" not in first
    assert "To do 2" in second and "Book the offsite" in second
    assert "Reply to Priya" not in third and "✓ 1 done this week" in third


def test_watching_changes_nothing_itself(shell, monkeypatch, clock):
    add("Reply to Priya")
    add("Book the offsite")
    store.set_state(elsewhere(), 1, State.DONE)  # leaves a gap for the next command to tidy
    before = store.data_version(mine := elsewhere())
    watching(monkeypatch, lambda: None)
    assert store.data_version(mine) == before  # it only looked, even on its way out
    assert [t.id for t in store.tasks(mine, [State.TODO])] == [2]
    assert "Renumbered: #2 is now #1" in todd("ls").output


def test_the_clock_alone_redraws_but_does_not_count_as_a_change(shell, monkeypatch, clock):
    add("Reply to Priya")

    def a_minute_later():
        clock["time"] = NOON.replace(minute=1, second=30)

    frames = watching(monkeypatch, a_minute_later)
    assert len(frames) == 2
    assert "updated 12:00:05" in frames[1]  # when the tasks last changed, not the minute


def test_a_deferred_task_appears_when_its_day_comes(shell, monkeypatch, clock):
    add("Reply to Priya")
    add("Renew the contract", defer_until=date(2026, 10, 1))

    def tomorrow():
        monkeypatch.setattr(cli, "_today", lambda: date(2026, 10, 1))

    first, second = watching(monkeypatch, tomorrow)
    assert "Renew the contract" not in first and "1 deferred" in first
    assert "Renew the contract" in second and "deferred" not in second


def test_filters_are_named_in_the_heading(shell, monkeypatch, clock):
    add("Reply to Priya", area="platform")
    add("Book the offsite", area="team")
    monkeypatch.setattr(cli, "_ticks", lambda every: iter([None]))
    out = todd("watch", "--area", "platform").output
    assert "todd watch · area platform · updated" in out
    assert "Reply to Priya" in out and "Book the offsite" not in out


def view_of(count: int) -> render.Watching:
    tasks = [Task(f"task {i}", state=State.TODO, id=i) for i in range(1, count + 1)]
    return render.Watching(render.Now(tasks, today=TODAY, now=NOON), NOON, fit=True)


def drawn(view: render.Watching, height: int) -> list[str]:
    console = Console(file=io.StringIO(), width=80, height=height, force_terminal=True)
    console.print(Screen(view))
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", console.file.getvalue())  # ty: ignore[unresolved-attribute]
    return [line.rstrip() for line in plain.splitlines()]


def test_on_a_short_screen_it_says_there_is_more():
    lines = drawn(view_of(14), height=12)
    assert len(lines) == 12
    assert lines[0].startswith("todd watch · updated 12:00:05")
    assert lines[-1] == "… 6 more lines: make this window taller, or see todd ls"
    roomy = drawn(view_of(3), height=12)
    assert not any("more line" in line for line in roomy) and "#3  task 3" in "\n".join(roomy)


def test_claude_does_not_plan_a_watch():
    group = cli.command_group()
    assert intent.check(group, ["watch"]) == "todd has no command 'watch'"
    assert not any(line.startswith("todd watch") for line in intent.reference(group).splitlines())


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_in_a_terminal_it_takes_the_screen_redraws_in_place_and_leaves_on_interrupt(tmp_path):
    database = tmp_path / "todd.sqlite"
    store.set_state(
        conn := db.connect(database), store.add(conn, Task("Reply to Priya")), State.TODO
    )
    env = {
        **os.environ,
        "TODD_DB": str(database),
        "TODD_CONFIG": str(tmp_path / "config.toml"),
        "TERM": "xterm-256color",
        "COLUMNS": "90",
        "LINES": "20",
    }
    master, slave = pty.openpty()
    command = [sys.executable, "-c", "from todd.cli import main; main()", "watch", "--every", "0.1"]
    watcher = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)

    def read_until(text: str, timeout: float = 15) -> str:
        seen, deadline = "", time.monotonic() + timeout
        while text not in seen and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    seen += os.read(master, 65536).decode(errors="replace")
                except OSError:
                    break
        assert text in seen, seen
        return seen

    try:
        first = read_until("Reply to Priya")
        assert "\x1b[?1049h" in first  # it has taken the screen
        store.set_state(conn, store.add(conn, Task("Book the offsite")), State.TODO)
        again = read_until("Book the offsite")
        assert "\x1b[?1049h" not in again and "To do" in again  # redrawn on the same screen
        watcher.send_signal(signal.SIGINT)  # Ctrl-C
        read_until("\x1b[?1049l")  # and given it back
        assert watcher.wait(timeout=10) == 0
    finally:
        if watcher.poll() is None:
            watcher.kill()
        os.close(master)
