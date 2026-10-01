"""Projects: work moved forward through tasks, some waiting on others."""

import copy

import pytest

from todd import cli, db, store, triage
from todd.errors import ToddError
from todd.models import State, Task, TaskRef, standing

from .conftest import PR_URL, SLACK_DM, answer, load
from .test_cli import named, saved, todd

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


def test_now_shows_only_what_you_can_act_on(tickets):
    launch(tickets)
    tickets.answers.append(answer(links=[], title="Loose end"))
    todd("add", "loose end")
    now = " ".join(todd().output.split())
    assert "Loose end" in now
    assert "Land the cluster-b stack" not in now  # waiting: not yours to act on
    assert "Set up the bug bash" not in now  # blocked
    assert "Not yours to act on now: 1 waiting · 2 blocked" in now
    assert "todd ls shows everything, by project" in now
    todd("done", "2", "-J")
    now = " ".join(todd("now").output.split())
    assert "Set up the bug bash ▸ Launch the live feature for Acme" in now  # says its project
    assert "1 blocked" in now


def test_ls_shows_everything_open_by_project(tickets):
    launch(tickets)
    tickets.answers.append(answer(links=[], title="Loose end"))
    todd("add", "loose end")
    out = todd("ls").output
    flat = " ".join(out.split())
    assert "▸ #1 Launch the live feature for Acme waiting · 3 of 3 tasks open" in flat
    assert "▸ No project 1" in flat
    assert out.index(TITLES[0]) < out.index(TITLES[1]) < out.index(TITLES[2]) < out.index("Loose")
    assert "blocked by #2 Land the cluster-b stack" in flat
    assert "PLAT-412 · stack 17 (3 PRs)" in flat and "PLAT-413" in flat  # in full, not cut off
    todd("done", "2", "-J")
    flat = " ".join(todd("ls").output.split())
    assert "Land the cluster-b stack" not in flat  # closed tasks only with --all
    assert "to do · 2 of 3 tasks open" in flat
    assert "Land the cluster-b stack" in todd("ls", "--all").output


def test_nothing_is_cut_off_however_narrow(tickets, monkeypatch):
    launch(tickets)
    monkeypatch.setattr(cli.out, "_width", 60)
    for command in (["ls"], ["projects"], ["show", "1"]):
        output = todd(*command).output
        assert max(len(line) for line in output.splitlines()) <= 60
        flat = " ".join(output.split())
        assert "Launch the live feature for Acme" in flat
        assert "Land the cluster-b stack" in flat
        assert "PLAT-412 · stack 17 (3 PRs)" in flat  # under the title: no room beside it
        assert "…" not in flat


def test_finishing_a_task_unblocks_the_next(tickets):
    launch(tickets)
    out = todd("done", "2", "-J").output
    assert "Unblocked: #3 Set up the bug bash" in out
    assert "#1 Launch the live feature for Acme is now to do" in out
    now = " ".join(todd().output.split())
    assert "Set up the bug bash" in now and "1 blocked" in now
    # Finishing #2 freed its number: the tasks after it moved down, and it's now #4.
    out = todd("reopen", "4", "-J").output
    assert "#2 Set up the bug bash waits on this again." in out


def test_starting_a_blocked_task_warns(tickets):
    launch(tickets)
    assert "#3 is still blocked by #2 Land the cluster-b stack" in todd("start", "3", "-J").output


def test_a_project_is_done_when_its_tasks_are(tickets, picks):
    launch(tickets)
    for _ in TITLES[:2]:
        todd("done", "2", "-J")  # each time, the next task has moved down to #2
    out = todd("done", "2", "-J").output
    assert "#1 Launch the live feature for Acme is now done: all 3 tasks finished" in out
    assert all(not question.startswith("That was the last") for question, _, _ in picks.asked)
    assert "PROJECT   DONE" in todd("show", "1").output
    assert "Launch the live feature" not in todd("projects").output
    assert "done · 0 of 3 tasks open" in todd("projects", "--all").output

    # Add a task to a finished project and it's open again. A new task waits on the project's
    # last open task by default; here there isn't one.
    tickets.answers.append(answer(links=[], title="Write the launch announcement"))
    todd("add", "write the launch announcement", "--in", "1")
    announcement = named("Write the launch announcement")
    assert announcement.id == 2  # the first number after what's open, which is the project
    assert (announcement.project_id, announcement.project_position) == (1, 4)
    assert announcement.blockers == []
    assert "to do · 1 of 4 tasks open" in todd("projects").output
    assert "is now done: all 4 tasks finished" in todd("done", "2", "-J").output


def test_a_projects_own_follow_ups_come_due_when_its_tasks_get_it_there(tickets):
    launch(tickets)
    todd("followup", "add", "1", "Tell", "sales", "it's", "live", "--when", "done")
    for _ in TITLES[:2]:
        assert "Follow-up due" not in todd("done", "2", "-J").output
    assert "Follow-up due: Tell sales it's live" in todd("done", "2", "-J").output


def test_tasks_can_go_on_at_the_same_time(shell):
    shell.answers.append(
        answer(
            title="Launch",
            links=[],
            tasks=[
                a_task("Land it", [], []),
                a_task("Write the docs", [], [1]),
                a_task("Brief support", [], [1]),
                a_task("Announce it", [], [2, 3]),
            ],
        )
    )
    todd("add", "land it, then docs and briefing support together, then announce")
    assert [[b.id for b in saved(n).blockers] for n in (2, 3, 4, 5)] == [[], [2], [2], [3, 4]]
    out = todd("done", "2", "-J").output
    assert "Unblocked: #3 Write the docs" in out and "Unblocked: #4 Brief support" in out
    now = todd().output
    assert "Write the docs" in now and "Brief support" in now and "Announce it" not in now
    todd("done", str(named("Write the docs").id), "-J")
    assert named("Announce it").blocked  # still waits on briefing support
    todd("done", str(named("Brief support").id), "-J")
    assert not named("Announce it").blocked


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


def test_a_project_always_has_a_task(shell):
    shell.answers.append(answer(title="Acme launch", links=[], tasks=[]))
    out = todd("add", "--project", "Acme launch").output
    assert "Filed #1 as a project with 1 task" in out
    assert "Give it at least one task" in shell.prompts[0]
    assert saved(1).is_project
    first = saved(2)
    assert first.title == "Pull the Q3 numbers from the migration dashboard"  # its next step
    assert (first.project_id, first.state) == (1, State.TODO)
    result = todd("edit", "2", "--in", "none")
    assert result.exit_code == 1
    assert "#2 is the only task in project #1, and a project has at least one task" in result.output


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
    assert "▸ #1 Launch the live feature for Acme  waiting · 3 of 3 tasks open" in out
    assert out.index(TITLES[0]) < out.index(TITLES[1]) < out.index(TITLES[2])
    assert "blocked by #2" in out


def test_dropping_a_project_drops_its_open_tasks(tickets, picks):
    launch(tickets)
    todd("done", "2", "-J")
    todd("drop", "1", "-J")  # Enter on the default: No
    assert picks.asked[-1][0] == "Drop “Launch the live feature for Acme” and its 2 open tasks?"
    assert picks.asked[-1][2] == "no"
    assert [named(title).state for title in TITLES[1:]] == [State.TODO, State.TODO]

    picks.script.append("yes")
    out = todd("drop", "1", "-J").output
    assert "#1 to do → dropped" in out and "#2 to do → dropped" in out
    assert [named(t).state for t in TITLES] == [State.DONE, State.DROPPED, State.DROPPED]
    assert "Launch the live feature" not in todd("projects").output
    assert "dropped · 0 of 3 tasks open" in todd("projects", "--all").output
    assert "Project #1 was dropped" in todd("add", "x", "--in", "1").output

    # Bringing it back brings back what was dropped with it, not what was already finished.
    out = todd("reopen", "1", "-J").output
    assert "#1 dropped → to do" in out
    assert [named(t).state for t in TITLES] == [State.DONE, State.TODO, State.TODO]


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


# ── A project's state comes from its tasks ─────────────────────────────────


def test_a_new_project_reads_as_its_first_task(tickets):
    launch(tickets)
    out = todd("show", "1").output
    assert "PROJECT   WAITING" in out  # the first task waits on reviews; the rest are blocked
    todd("start", "2", "-J")
    assert "PROJECT   DOING" in todd("show", "1").output


def test_a_project_is_never_moved_itself(tickets):
    launch(tickets)
    for verb in ("start", "wait", "review", "done", "follow", "reopen"):
        result = todd(verb, "1")
        assert result.exit_code == 1
        assert "#1 is a project: its state comes from its tasks" in result.output
    assert saved(1).state == State.TODO


def test_following_stays_outside_projects(tickets):
    launch(tickets)
    result = todd("follow", "3")
    assert result.exit_code == 1
    assert "#3 is part of project #1, and following is for things outside projects" in result.output
    tickets.answers.append(answer(links=[], title="The X refactor", track="following"))
    todd("add", "following the X refactor")
    assert saved(5).state == State.FOLLOWING
    assert "stays outside projects" in todd("edit", "5", "--in", "1").output
    # Said to be in a project, it's simply a task to do there.
    tickets.answers.append(answer(links=[], title="The Y refactor", track="following"))
    todd("add", "following the Y refactor", "--in", "1")
    assert saved(6).state == State.TODO


def test_something_followed_is_never_split_into_a_project():
    filing = triage.parse(
        answer(track="following", tasks=[a_task("One", [], []), a_task("Two", [], [1])]),
        fallback_title="x",
        n_links=0,
    )
    assert not filing.is_project and filing.track == State.FOLLOWING


@pytest.mark.parametrize(
    ("states", "blocked", "expected"),
    [
        ([State.DOING, State.WAITING], [], "doing"),
        ([State.IN_REVIEW, State.TODO], [], "in review"),
        ([State.WAITING, State.TODO], [], "to do"),
        ([State.WAITING, State.TODO], [1], "waiting"),  # the to-do one is blocked
        ([State.TODO, State.TODO], [0, 1], "blocked"),
        ([State.INBOX], [], "to do"),
        ([State.DONE, State.DROPPED], [], "done"),
        ([State.DROPPED, State.DROPPED], [], "dropped"),
        ([State.DONE, State.WAITING], [], "waiting"),
    ],
)
def test_standing(states, blocked, expected):
    tasks = [
        Task(f"t{i}", state=state, blockers=[TaskRef(99, "x", State.TODO)] if i in blocked else [])
        for i, state in enumerate(states)
    ]
    assert standing(Task("p", is_project=True), tasks).label == expected


def test_a_dropped_project_stays_dropped_whatever_its_tasks_say():
    project = Task("p", is_project=True, state=State.DROPPED)
    assert standing(project, [Task("t", state=State.DOING)]).label == "dropped"
