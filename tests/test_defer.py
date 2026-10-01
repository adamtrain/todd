"""Defer dates: until its date, a task is out of `todd now`. A project is deferred for as long
as every task of its that could be worked on is."""

from datetime import date, timedelta

import pytest

from todd import cli, db, intent, triage
from todd.models import State, Task, TaskRef, standing
from todd.people import Nicknames

from .conftest import TODAY, answer
from .test_cli import named, saved, todd
from .test_projects import a_task

MONDAY = date(2026, 10, 5)  # TODAY is Wednesday 2026-09-30


def task(shell, title: str, **filed) -> None:
    shell.answers.append(answer(title=title, links=[], due=None, due_hint=None, **filed))
    todd("add", title.lower(), "-y")


def launch(shell) -> None:
    """#1 Launch: #2 Land it, then #3 Bash it; #4 Write docs can start any time."""
    shell.answers.append(
        answer(
            title="Launch",
            links=[],
            tasks=[
                a_task("Land it", [], []),
                a_task("Bash it", [], [1]),
                a_task("Write docs", [], []),
            ],
        )
    )
    todd("add", "land it, then bash it; docs whenever", "-y")


def test_a_deferred_task_is_out_of_now_but_still_in_ls(shell, monkeypatch):
    task(shell, "Renew the contract")
    task(shell, "Reply to Priya")
    out = todd("defer", "1", "mon").output
    assert "#1 deferred until Mon Oct 5  Renew the contract" in out
    assert saved(1).defer_until == MONDAY and saved(1).state == State.TODO

    now = todd().output
    assert "Renew the contract" not in now and "Reply to Priya" in now
    assert "Not yours to act on now: 1 deferred" in now
    listed = " ".join(todd("ls").output.split())
    assert "Renew the contract deferred" in listed and "deferred until Mon" in listed
    assert "Deferred until Mon Oct 5" in todd("show", "1").output
    assert "Deferred until 2026-10-05" in [e.text for e in saved(1).entries]

    # On the day, it's back, with nothing to undo.
    monkeypatch.setattr(cli, "_today", lambda: MONDAY)
    now = todd().output
    assert "Renew the contract" in now and "deferred" not in now
    assert "deferred" not in todd("ls").output


def test_deferring_can_be_undone_and_starting_ends_it(shell):
    task(shell, "Renew the contract")
    todd("defer", "1", "+14")
    assert "#1 is no longer deferred" in todd("defer", "1", "none").output
    assert saved(1).defer_until is None
    todd("defer", "1", "2026-11-02")
    out = todd("start", "1", "--local").output
    assert "No longer deferred." in out
    assert saved(1).defer_until is None and saved(1).state == State.DOING


def test_what_cannot_be_deferred(shell):
    launch(shell)
    result = todd("defer", "1", "mon")
    assert result.exit_code == 1
    assert "#1 is a project, and a project isn't deferred itself" in result.output
    assert "isn't deferred itself" in todd("edit", "1", "--defer", "mon").output
    assert "isn't a date todd understands" in todd("defer", "2", "someday").output
    assert "isn't in the future, so #2 stays" in todd("defer", "2", "today").output
    todd("done", "2", "--local")
    assert "is done, so there's nothing to put off" in todd("defer", "4", "mon").output


def test_a_task_can_be_both_blocked_and_deferred(shell):
    launch(shell)
    todd("defer", "3", "mon")  # Bash it, which waits on Land it
    listed = " ".join(todd("ls").output.split())
    assert "blocked by #2 Land it · deferred until Mon" in listed
    assert "Not yours to act on now: 1 blocked" in todd().output  # counted once


def test_edit_can_set_and_clear_it(shell):
    task(shell, "Renew the contract")
    assert "Deferred until Mon Oct 5" in todd("edit", "1", "--defer", "mon").output
    assert saved(1).defer_until == MONDAY
    todd("edit", "1", "--defer", "none")
    assert saved(1).defer_until is None


def test_a_project_is_deferred_while_everything_workable_is(shell):
    launch(shell)
    assert "PROJECT   TO DO" in todd("show", "1").output
    out = todd("defer", "2", "+10").output  # Land it: Bash it is blocked behind it
    assert "#1 Launch is now" not in out  # Write docs can still be worked on
    out = todd("defer", "4", "mon").output  # …and now it can't
    assert "#1 Launch is now deferred until Mon" in out
    shown = todd("show", "1").output
    assert "PROJECT   DEFERRED" in shown
    assert "Deferred until Mon Oct 5 (when its next task comes back)" in shown  # the soonest
    listed = " ".join(todd("ls").output.split())
    assert "▸ #1 Launch deferred until Mon · 3 of 3 tasks open" in listed
    assert "Launch" not in todd().output
    assert "Not yours to act on now: 1 blocked · 2 deferred" in todd().output

    # Bringing either back brings the project back.
    assert "#1 Launch is now to do" in todd("defer", "2", "none").output
    assert "Land it  ▸ Launch" in " ".join(todd().output.split()).replace(" ▸", "  ▸")


def test_a_project_waits_for_the_soonest_of_several(shell, monkeypatch):
    launch(shell)
    todd("defer", "2", "2026-10-20")
    todd("defer", "4", "2026-10-12")
    assert "deferred until Oct 12" in todd("projects").output
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 10, 12))
    out = todd("projects").output
    assert "Launch  to do" in out and "deferred until Oct 20" in out  # just the one task now


@pytest.mark.parametrize(
    ("tasks", "label", "until"),
    [
        ([("todo", None, False)], "to do", None),
        ([("todo", 5, False)], "deferred", 5),
        ([("todo", 9, False), ("todo", 5, False)], "deferred", 5),
        ([("todo", 5, False), ("waiting", None, False)], "waiting", None),
        ([("todo", 5, False), ("todo", None, True)], "deferred", 5),  # the other is blocked
        ([("todo", 5, True)], "blocked", None),  # blocked first: its date isn't what holds it
        ([("todo", 0, False)], "to do", None),  # today counts as back
        ([("done", None, False), ("todo", 5, False)], "deferred", 5),
    ],
)
def test_standing_with_deferred_tasks(tasks, label, until):
    made = [
        Task(
            f"t{i}",
            state=State(state),
            defer_until=TODAY + timedelta(days=days) if days is not None else None,
            blockers=[TaskRef(99, "x", State.TODO)] if blocked else [],
        )
        for i, (state, days, blocked) in enumerate(tasks)
    ]
    where = standing(Task("p", is_project=True), made, TODAY)
    assert where.label == label
    assert where.until == (TODAY + timedelta(days=until) if until is not None else None)


def test_claude_files_a_defer_date_when_you_say_one(shell):
    task(shell, "Renew the contract", defer="2026-10-05")
    assert saved(1).defer_until == MONDAY
    assert "Deferred until Mon Oct 5" in todd("show", "1").output
    schema = triage.SCHEMA
    assert "defer" in schema["required"]
    assert "defer" in schema["properties"]["tasks"]["items"]["required"]
    assert "A project is never deferred itself" in triage.SYSTEM


def test_a_project_filed_with_a_defer_date_puts_it_on_the_tasks_that_could_start(shell):
    shell.answers.append(
        answer(
            title="Launch",
            links=[],
            defer="2026-10-05",
            tasks=[
                a_task("Land it", [], []),
                a_task("Bash it", [], [1]),
                a_task("Write docs", [], [], defer="2026-10-20"),
            ],
        )
    )
    todd("add", "launch, not before monday", "-y")
    assert saved(1).is_project and saved(1).defer_until is None
    assert [named(t).defer_until for t in ("Land it", "Bash it", "Write docs")] == [
        MONDAY,
        None,  # it waits on Land it anyway
        date(2026, 10, 20),  # Claude's own date for it stands
    ]
    assert "deferred until Mon" in todd("projects").output


def test_claude_is_told_what_is_deferred(shell):
    launch(shell)
    todd("defer", "2", "mon")
    todd("defer", "4", "mon")
    text = intent.context(db.connect(db.db_path()), TODAY, Nicknames())
    assert "#1 [project · deferred until 2026-10-05] Launch" in text
    assert (
        "#2 [to do] Land it · in project #1 “Launch” · area platform · deferred until 2026-10-05"
        in text
    )
    assert "todd defer TASK_ID UNTIL..." in intent.reference(cli.command_group())
    assert "Deferred: Not a state either" in intent.glossary.as_text()


# ── Due dates say what they are ────────────────────────────────────────────


def test_lists_say_due_before_a_deadline(shell):
    task(shell, "Reply to Priya")
    task(shell, "Fix the deploy job")
    task(shell, "Book the offsite")
    todd("edit", "1", "--due", "tomorrow")
    todd("edit", "2", "--due", "2026-09-28")
    todd("edit", "3", "--due", "2026-10-20")
    for command in ("now", "ls"):
        out = todd(command).output
        assert "due tomorrow" in out and "due Oct 20" in out
        assert "overdue 2d" in out and "due overdue" not in out
