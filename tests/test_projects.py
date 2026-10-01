"""Projects: work moved forward through tasks, some waiting on others."""

import copy

import pytest

from todd import db, store, triage
from todd.errors import ToddError
from todd.models import State, Task

from .conftest import PR_URL, SLACK_DM, answer, load
from .test_cli import saved, todd

TITLES = [
    "Land the cluster-b stack",
    "Set up the bug bash",
    "Configure the live feature for Acme",
]


def a_task(title: str, links: list[int], after: list[int], **changes) -> dict:
    return {
        "title": title,
        "needs_title": False,
        "next_action": f"Next step for {(title or 'it').lower()}",
        "track": "todo",
        "kind": "do",
        "priority": "normal",
        "due": None,
        "due_hint": None,
        "people": [],
        "waiting_on": None,
        "follow_ups": [],
        "links": links,
        "after": after,
        **changes,
    }


@pytest.fixture
def tickets(shell):
    for key, summary in (("PLAT-413", "Bug bash"), ("PLAT-414", "Enable for Acme")):
        ticket = copy.deepcopy(load("acli_view_PLAT-412"))
        ticket["key"] = key
        ticket["fields"]["summary"] = summary
        ticket["fields"]["status"]["name"] = "To Do"
        shell.tickets[key] = ticket
    return shell


def launch(shell) -> None:
    """Your example: a stack waiting on review, then a bug bash, then the live feature.

    Links as Claude sees them: 1-3 the stack (85, 86, 88), 4 PLAT-412, 5 PLAT-413, 6 PLAT-414.
    """
    shell.answers.append(
        answer(
            title="Launch the live feature for Acme",
            next_action="Chase reviews on the cluster-b stack",
            area="billing",
            links=[],
            tasks=[
                a_task(TITLES[0], [2, 4], [], track="waiting", waiting_on="reviews on the stack"),
                a_task(TITLES[1], [5], [1]),
                a_task(TITLES[2], [6], [2]),
            ],
        )
    )
    result = todd(
        "add",
        "Waiting on reviews for this stack",
        PR_URL,
        "PLAT-412",
        "then a bug bash",
        "PLAT-413",
        "then configure the live feature for Acme",
        "PLAT-414",
    )
    assert result.exit_code == 0, result.output


def test_a_described_chain_becomes_a_project_with_tasks(tickets):
    launch(tickets)
    project = saved(1)
    assert project.is_project and project.state == State.TODO
    assert project.title == "Launch the live feature for Acme"
    assert project.links == []  # every link went to the task it belongs to

    first, second, third = (saved(n) for n in (2, 3, 4))
    assert [t.title for t in (first, second, third)] == TITLES
    assert [(t.project_id, t.project_position) for t in (first, second, third)] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
    assert first.state == State.WAITING and first.waiting_on == "reviews on the stack"
    assert first.area == "billing"
    # The whole stack goes with the task, though Claude only named one of its pull requests.
    assert [link.ref for link in first.links] == [
        "acme/billing#85",
        "acme/billing#86",
        "acme/billing#88",
        "PLAT-412",
    ]
    assert [link.ref for link in second.links] == ["PLAT-413"]
    assert [link.ref for link in third.links] == ["PLAT-414"]
    assert [b.id for b in second.blockers] == [2]
    assert [b.id for b in third.blockers] == [3]
    assert second.blocked and third.blocked and not first.blocked


def test_what_capturing_a_project_shows(tickets):
    launch(tickets)
    out = todd("show", "1").output
    assert "PROJECT" in out and "Launch the live feature for Acme" in out
    assert "Tasks · 3 of 3 tasks open" in out
    assert "blocked by #2" in out and "blocked by #3" in out


def test_the_queue_shows_what_you_can_do_now(tickets):
    launch(tickets)
    queue = " ".join(todd().output.split())
    assert "Land the cluster-b stack ▸ Launch the live" in queue  # (the title wraps)
    assert "Set up the bug bash" not in queue  # blocked: it waits under its project
    assert "2 blocked (todd projects)" in queue
    assert "Projects needing a next task" not in queue


def test_finishing_a_task_unblocks_the_next(tickets):
    launch(tickets)
    out = todd("done", "2", "-J").output
    assert "Unblocked: #3 Set up the bug bash" in out
    queue = " ".join(todd().output.split())
    assert "Set up the bug bash" in queue and "1 blocked" in queue
    out = todd("reopen", "2", "-J").output
    assert "#3 Set up the bug bash waits on this again." in out


def test_starting_a_blocked_task_warns(tickets):
    launch(tickets)
    assert "#3 is still blocked by #2 Land the cluster-b stack" in todd("start", "3", "-J").output


def test_the_last_task_asks_whether_the_project_is_done(tickets, monkeypatch, picks):
    launch(tickets)
    for task_id in (2, 3):
        todd("done", str(task_id), "-J")
    out = todd("done", "4", "-J").output  # Enter on the default: No
    assert picks.asked[-1][0].startswith("That was the last open task in “Launch the live")
    assert picks.asked[-1][2] == "no"
    assert 'needs a next task: todd add "…" --in 1' in out
    assert saved(1).state == State.TODO
    assert "Projects needing a next task 1" in todd().output

    # A new task in the project waits on its last open task by default; here there isn't one.
    tickets.answers.append(answer(links=[], title="Write the launch announcement"))
    todd("add", "write the launch announcement", "--in", "1")
    announcement = saved(5)
    assert (announcement.project_id, announcement.project_position) == (1, 4)
    assert announcement.blockers == []
    assert "Projects needing a next task" not in todd().output

    picks.script.append("yes")
    todd("done", "5", "-J")
    assert saved(1).state == State.DONE


def test_adding_to_a_project_waits_on_its_last_open_task(tickets):
    launch(tickets)
    tickets.answers.append(answer(links=[], title="Tell sales it's live"))
    out = todd("add", "tell sales it's live", "--in", "1").output
    assert "task 4 of #1 Launch the live feature for Acme" in out
    assert [b.id for b in saved(5).blockers] == [4]
    tickets.answers.append(answer(links=[], title="Order cake"))
    todd("add", "order cake", "--in", "1", "--after", "none")
    assert saved(6).blockers == []
    tickets.answers.append(answer(links=[], title="Dry run"))
    todd("add", "dry run", "--in", "1", "--after", "2,3")
    assert [b.id for b in saved(7).blockers] == [2, 3]


def test_adding_to_something_that_isnt_a_project(shell):
    shell.answers.append(answer())
    todd("add", "a plain task", SLACK_DM, "-q", "hi")
    result = todd("add", "x", "--in", "1")
    assert result.exit_code == 1 and "#1 isn't a project" in result.output
    assert "not both" in todd("add", "x", "--in", "1", "--project").output
    assert "--after only means something with --in" in todd("add", "x", "--after", "2").output


def test_a_project_without_tasks_yet(shell):
    shell.answers.append(answer(title="Acme launch", links=[], tasks=[]))
    out = todd("add", "--project", "Acme launch").output
    assert "Filed #1 as a project with 0 tasks" in out
    assert "The person says this is a project" in shell.prompts[0]
    assert saved(1).is_project
    queue = todd().output
    assert "Projects needing a next task 1" in queue and 'todd add "…" --in 1' in queue


def test_refiling_never_splits(tickets):
    launch(tickets)
    tickets.answers.append(answer(tasks=[a_task("Surprise", [], [])], links=[]))
    todd("triage", "3")
    assert "keep tasks empty" in tickets.prompts[-1]
    assert len(store.tasks(db.connect(db.db_path()))) == 4


def test_block_and_unblock_by_hand(tickets):
    launch(tickets)
    result = todd("block", "2", "--on", "4")
    assert result.exit_code == 1 and "#4 already waits on #2" in result.output
    assert "#2 waits on #3 Set up the bug bash" not in todd("block", "2", "--on", "2").output
    tickets.answers.append(answer(links=[], title="Unrelated"))
    todd("add", "unrelated")
    assert "#5 waits on #2 Land the cluster-b stack" in todd("block", "5", "--on", "2").output
    assert saved(5).blocked
    assert "#5 no longer waits on anything" in todd("unblock", "5").output
    assert "wasn't waiting" in todd("unblock", "5").output
    assert not saved(5).blocked


def test_projects_lists_each_project_and_its_tasks(tickets):
    launch(tickets)
    out = todd("projects").output
    assert "▸ #1 Launch the live feature for Acme  3 of 3 tasks open" in out
    assert out.index(TITLES[0]) < out.index(TITLES[1]) < out.index(TITLES[2])
    assert "blocked by #2" in out
    todd("drop", "1", "-J")
    assert "Launch the live feature" not in todd("projects").output
    assert (
        "Launch the live feature for Acme  3 of 3 tasks open · dropped"
        in todd("projects", "--all").output
    )


def test_moving_a_task_into_a_project(tickets):
    launch(tickets)
    tickets.answers.append(answer(links=[], title="Loose end"))
    todd("add", "loose end")
    todd("edit", "5", "--in", "1")
    assert (saved(5).project_id, saved(5).project_position) == (1, 4)
    todd("edit", "5", "--in", "none")
    assert saved(5).project_id is None
    assert "isn't a project" in todd("edit", "5", "--in", "2").output


# ── Parsing and storing ────────────────────────────────────────────────────


def test_parse_keeps_only_sensible_link_numbers_and_orderings():
    filing = triage.parse(
        answer(
            tasks=[
                a_task(
                    "One", [1, 9, 0], [1, 2]
                ),  # 9 and 0 aren't links; a task can't wait on itself
                a_task("Two", [2], [1, 5]),  # there's no task 5
            ]
        ),
        fallback_title="x",
        n_links=2,
    )
    one, two = filing.tasks
    assert (one.link_indexes, one.after) == ([1], [2])
    assert (two.link_indexes, two.after) == ([2], [1])
    assert filing.is_project


def test_an_untitled_task_in_a_project_needs_a_title():
    filing = triage.parse(
        answer(tasks=[a_task(None, [], [])]),  # ty: ignore[invalid-argument-type]
        fallback_title="x",
        n_links=0,
    )
    assert filing.tasks[0].needs_title
    assert filing.tasks[0].title == "Task 1 of Send Priya the Q3 migration numbers"


def test_the_schema_and_prompt_explain_projects():
    assert "tasks" in triage.SCHEMA["required"]
    task_schema = triage.SCHEMA["properties"]["tasks"]["items"]
    assert {"links", "after"} <= set(task_schema["required"])
    assert "Only list tasks the capture names" in triage.SYSTEM


def test_blockers_refuse_loops(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    a, b, c = (store.add(conn, Task(title)) for title in "abc")
    with db.tx(conn):
        store.add_blocker(conn, b, a)
        store.add_blocker(conn, c, b)
    with pytest.raises(ToddError, match="already waits on"), db.tx(conn):
        store.add_blocker(conn, a, c)
    with pytest.raises(ToddError, match="can't wait on itself"), db.tx(conn):
        store.add_blocker(conn, a, a)
    assert [t.id for t in store.dependents(conn, a)] == [b]


def test_a_dropped_blocker_is_out_of_the_way(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    a, b = store.add(conn, Task("a")), store.add(conn, Task("b"))
    with db.tx(conn):
        store.add_blocker(conn, b, a)
    assert store.get(conn, b).blocked
    store.set_state(conn, a, State.DROPPED)
    assert not store.get(conn, b).blocked
