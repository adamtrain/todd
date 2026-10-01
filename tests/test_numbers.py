"""Numbers are reused: what's open is numbered from 1 with no gaps, and what's closed comes
after, most recently closed first."""

import sqlite3

import pytest

from todd import db, store
from todd.models import Followup, FollowupStatus, Link, LinkKind, State, Task

from .conftest import answer
from .test_cli import named, saved, todd
from .test_intent import plan
from .test_projects import a_task


def five(shell) -> list[str]:
    titles = ["One", "Two", "Three", "Four", "Five"]
    for title in titles:
        todd("add", "--raw", title)
    return titles


def numbers(*titles: str) -> list[int]:
    return [named(title).id for title in titles]


# ── In the database ────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def test_renumbering_takes_everything_that_points_at_a_task_with_it(conn):
    project = store.add(conn, Task("Project", is_project=True))
    first, second, third = (
        store.add(
            conn,
            Task(
                title,
                project_id=project,
                project_position=i,
                links=[Link(LinkKind.JIRA, None, ref=f"PLAT-{i}")],
            ),
        )
        for i, title in enumerate(("first", "second", "third"), 1)
    )
    with db.tx(conn):
        store.add_blocker(conn, second, first)
        store.add_blocker(conn, third, second)
        store.set_people(conn, second, ["Priya"])
    store.add_followup(conn, second, Followup(action="Tell Theo"))
    store.note(conn, second, "a note")
    assert store.renumber(conn).tasks == {}  # nothing to do yet

    store.set_state(conn, first, State.DONE)
    moved = store.renumber(conn)
    assert moved.tasks == {2: 4, 3: 2, 4: 3}
    assert moved.now.open_tasks == {1, 2, 3}
    second = store.get(conn, 2)
    assert second.title == "second" and second.project_id == 1
    assert [link.ref for link in second.links] == ["PLAT-2"]
    assert second.people == ["Priya"]
    assert [f.action for f in second.followups] == ["Tell Theo"]
    assert "a note" in [e.text for e in second.entries]
    assert [b.id for b in second.blockers] == [4]  # the finished one, under its new number
    assert [b.id for b in store.get(conn, 3).blockers] == [2]
    assert [t.title for t in store.project_tasks(conn, 1)] == ["first", "second", "third"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.renumber(conn).tasks == {}  # and it's settled


def test_closed_things_come_after_open_ones_most_recently_closed_first(conn):
    a, b, c, d = (store.add(conn, Task(title)) for title in "abcd")
    store.set_state(conn, a, State.DONE)
    conn.execute("UPDATE task SET state_at = '2026-09-01T10:00:00Z' WHERE id = ?", (a,))
    store.set_state(conn, c, State.DROPPED)
    conn.execute("UPDATE task SET state_at = '2026-09-02T10:00:00Z' WHERE id = ?", (c,))
    store.renumber(conn)
    assert [t.title for t in store.tasks(conn)] == ["b", "d", "c", "a"]
    assert (b, d) == (2, 4)  # as they were; now 1 and 2


def test_a_project_closes_when_its_last_task_does(conn):
    project = store.add(conn, Task("Project", is_project=True))
    task = store.add(conn, Task("only task", project_id=project, project_position=1))
    other = store.add(conn, Task("other"))
    store.move(conn, project, State.TODO)
    store.set_state(conn, task, State.DONE)
    store.renumber(conn)
    assert [t.title for t in store.tasks(conn)] == ["other", "Project", "only task"]
    assert other == 3  # was


def test_follow_ups_are_numbered_the_same_way(conn):
    task = store.add(conn, Task("a"))
    one, two, three = (
        store.add_followup(conn, task, Followup(action=action)) for action in ("x", "y", "z")
    )
    store.close_followup(conn, one, FollowupStatus.DONE)
    moved = store.renumber(conn)
    assert moved.followups == {1: 3, 2: 1, 3: 2}
    assert [(f.id, f.action) for f in store.open_followups(conn)] == [(1, "y"), (2, "z")]
    assert store.get_followup(conn, 3).status == FollowupStatus.DONE
    assert (two, three) == (2, 3)  # as they were


# ── As you use it ──────────────────────────────────────────────────────────


def test_finishing_something_frees_its_number(shell):
    titles = five(shell)
    out = todd("done", "2", "--local").output
    assert "#2 inbox → done  Two" in out
    assert "Renumbered: #2 is now #5 · #3 to #5 are now #2 to #4" in out
    assert numbers(*titles) == [1, 5, 2, 3, 4]
    listed = todd("ls").output
    assert [f"#{n}" in listed for n in (1, 2, 3, 4, 5)] == [True, True, True, True, False]

    out = todd("drop", "1", "--local").output
    assert "Renumbered: #1 is now #4 · #2 to #4 are now #1 to #3" in out
    # Open ones first, in the order they had; then closed ones, the latest first.
    assert numbers("Three", "Four", "Five", "One", "Two") == [1, 2, 3, 4, 5]


def test_finishing_the_last_one_moves_nothing(shell):
    five(shell)
    assert "Renumbered" not in todd("done", "5", "--local").output
    assert saved(5).title == "Five"


def test_a_new_task_takes_the_first_number_after_the_open_ones(shell):
    five(shell)
    todd("done", "2", "--local")
    todd("done", "2", "--local")  # Three, which moved down
    shell.answers.append(answer(links=[], title="Six"))
    out = todd("add", "six", "-y").output
    assert "Filed #4 in to do" in out and "#4" in out
    assert "Renumbered" not in out  # nobody was shown another number for it
    assert numbers("One", "Four", "Five", "Six", "Three", "Two") == [1, 2, 3, 4, 5, 6]
    assert "Saved #5 to your inbox" in todd("add", "--raw", "Seven").output


def test_reopening_what_you_just_closed_changes_no_numbers(shell):
    titles = five(shell)
    todd("done", "2", "--local")
    out = todd("reopen", "5", "--local").output
    assert "#5 done → to do  Two" in out and "Renumbered" not in out
    assert numbers(*titles) == [1, 5, 2, 3, 4]


def test_reopening_something_older_brings_it_to_the_end_of_the_open_ones(shell):
    titles = five(shell)
    todd("done", "1", "--local")
    todd("done", "1", "--local")
    assert numbers(*titles) == [5, 4, 1, 2, 3]
    out = todd("reopen", "5", "--local").output
    # (Two, still closed, moves along behind it; that isn't worth saying.)
    assert "Renumbered: #5 is now #4" in out and "#4 is now" not in out
    assert numbers(*titles) == [4, 5, 1, 2, 3]


def test_a_new_projects_tasks_follow_on_from_the_open_ones(shell):
    five(shell)
    todd("done", "1", "--local")
    shell.answers.append(
        answer(title="Launch", links=[], tasks=[a_task("Land it", [], []), a_task("Tell", [], [1])])
    )
    out = todd("add", "land it then tell people", "-y").output
    assert "Filed #5 as a project with 2 tasks" in out and "Renumbered" not in out
    assert numbers("Launch", "Land it", "Tell", "One") == [5, 6, 7, 8]
    assert [b.id for b in named("Tell").blockers] == [6]
    assert "Filed by Claude as task 1 of “Launch”" in [e.text for e in named("Land it").entries]


def test_a_finished_project_gives_up_its_number_and_gets_one_back(shell):
    shell.answers.append(answer(title="Launch", links=[], tasks=[a_task("Land it", [], [])]))
    todd("add", "land it", "-y")
    todd("add", "--raw", "Loose end")
    out = todd("done", "2", "--local").output
    assert "#1 Launch is now done" in out
    assert "Renumbered: #1 to #2 are now #2 to #3 · #3 is now #1" in out
    assert numbers("Loose end", "Launch", "Land it") == [1, 2, 3]
    # A new task in it opens it again, and both come back among the open ones.
    shell.answers.append(answer(links=[], title="Announce it"))
    out = todd("add", "announce it", "--in", "2", "-y").output
    assert "Filed #3 in to do" in out
    assert numbers("Loose end", "Launch", "Announce it", "Land it") == [1, 2, 3, 4]


def test_follow_ups_reuse_numbers_too(shell):
    five(shell)
    todd("followup", "add", "1", "Tell", "Theo")
    todd("followup", "add", "2", "Ask", "Mike")
    out = todd("followup", "done", "1").output
    assert "↪1 Tell Theo" in out
    assert "Renumbered: ↪1 is now ↪2 · ↪2 is now ↪1" in out
    assert "↪2 Ask Priya" in todd("followup", "add", "3", "Ask", "Priya").output
    listed = todd("followup").output
    assert "↪1" in listed and "Ask Mike" in listed and "↪3" not in listed


def test_a_plan_settles_once_its_steps_have_run(shell, picks):
    titles = five(shell)
    shell.answers.append(
        plan(
            (["done", "1", "--local"], "Finish #1 One"),
            (["done", "3", "--local"], "Finish #3 Three"),
            (["start", "5", "--local"], "Start #5 Five"),
        )
    )
    out = todd("finish one and three, then start five").output
    # Every step meant the number it had when the plan was made.
    assert [named(t).state for t in titles] == [
        State.DONE,
        State.INBOX,
        State.DONE,
        State.INBOX,
        State.DOING,
    ]
    assert out.count("Renumbered") == 1
    assert out.index("#5 inbox → doing") < out.index("Renumbered")
    assert numbers("Two", "Four", "Five", "Three", "One") == [1, 2, 3, 4, 5]


def test_a_plan_that_just_adds_something_shows_its_final_number(shell, picks):
    five(shell)
    todd("done", "1", "--local")
    shell.answers += [
        plan((["add", "call Mike"], "Add a task: call Mike")),
        answer(links=[], title="Call Mike"),
    ]
    out = todd("remind me to call Mike").output
    assert "Filed #5 in to do" in out and "Renumbered" not in out
    assert named("Call Mike").id == 5


def test_a_list_with_gaps_is_tidied_when_you_next_look(shell):
    titles = five(shell)
    conn = sqlite3.connect(db.db_path())
    conn.execute("DELETE FROM task WHERE id IN (2, 3)")  # as an older todd left things
    conn.commit()
    out = todd("ls").output
    assert out.index("Renumbered: #4 to #5 are now #2 to #3") < out.index("Four")
    assert numbers(titles[0], *titles[3:]) == [1, 2, 3]


def test_the_timeline_names_tasks_by_title_not_number(shell):
    five(shell)
    todd("block", "3", "--on", "1")
    todd("unblock", "3", "--on", "1")
    notes = [e.text for e in saved(3).entries]
    assert "Waits on “One”" in notes and "No longer waits on “One”" in notes


def test_states_explains_numbers(shell):
    out = " ".join(todd("states").output.split())
    assert "Numbers" in out and "the ones after it move down to fill its place" in out
