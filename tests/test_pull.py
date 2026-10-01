"""`todd pull`: bring a task up to date with its links."""

from todd import pull
from todd.models import Link, LinkKind, Reviewer, ReviewState, State

from .conftest import SLACK_DM, answer
from .test_cli import named, saved, todd


def proposal(**changes) -> dict:
    return {
        "state": "done",
        "next_action": "Tell Priya it's shipped",
        "waiting_on": None,
        "reason": "PLAT-412 moved to Done",
        **changes,
    }


def filed(shell) -> None:
    """#1: a reply to Priya, with its ticket PLAT-412 (In Progress)."""
    todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", "numbers?", "-y")


def ticket_moves_to(shell, status: str) -> None:
    shell.tickets["PLAT-412"]["fields"]["status"]["name"] = status


def test_what_counts_as_a_change():
    link = Link(LinkKind.JIRA, None, ref="PLAT-1", status="Done", title="T")
    assert pull.changes(("In Progress", "T", frozenset()), link) == ["status In Progress → Done"]
    assert pull.changes(None, link) == ["newly linked"]
    assert pull.changes(pull.look(link), link) == []
    pr = Link(
        LinkKind.GITHUB,
        "u",
        ref="a/b#1",
        status="open",
        reviewers=[Reviewer("sam", ReviewState.APPROVED), Reviewer("nik", ReviewState.REQUESTED)],
    )
    before = ("open", None, frozenset({("sam", "requested"), ("luke", "requested")}))
    assert pull.changes(before, pr) == [
        "nik: requested",
        "sam: requested → approved",
        "luke no longer reviewing",
    ]


def test_pulling_a_task_proposes_and_applies(shell, picks):
    filed(shell)
    ticket_moves_to(shell, "Done")
    shell.answers.append(proposal())
    out = todd("pull", "1").output
    assert "status In Progress → Done" in out
    assert "Pull would update #1 · not saved yet" in out
    flat = " ".join(out.split())
    assert "state to do → done" in flat and "why PLAT-412 moved to Done" in flat
    assert picks.asked[0][:2] == ("Update it?", ["apply", "change", "skip"])
    task = saved()
    assert task.state == State.DONE
    assert task.next_action == "Tell Priya it's shipped"
    assert "Pulled: PLAT-412 moved to Done" in [e.text for e in task.entries]
    assert shell.transitions == []  # Jira already says Done: nothing to move

    prompt = shell.prompts[-1]
    assert "Since todd last looked: status In Progress → Done" in prompt
    assert "doing → In Progress; in_review → In Review; done → Done" in prompt


def test_asking_for_a_different_update(shell, picks):
    filed(shell)
    ticket_moves_to(shell, "Done")
    shell.answers += [
        proposal(),
        proposal(state="in_review", reason="Done in Jira, but not shipped"),
    ]
    picks.script += ["change", "apply"]
    todd("pull", "1", input="it's only in review until it ships\n")
    assert "<previous_proposal>" in shell.prompts[-1]
    assert "<change>it's only in review until it ships</change>" in shell.prompts[-1]
    assert saved().state == State.IN_REVIEW


def test_skipping_leaves_it(shell, picks):
    filed(shell)
    ticket_moves_to(shell, "Done")
    shell.answers.append(proposal())
    picks.script.append("skip")
    todd("pull", "1")
    assert saved().state == State.TODO


def test_without_a_terminal_it_only_proposes(shell):
    filed(shell)
    ticket_moves_to(shell, "Done")
    shell.answers.append(proposal())
    out = todd("pull", "1").output
    assert "Left it as it was." in out and "todd pull 1 -y" in out
    assert saved().state == State.TODO
    shell.answers.append(proposal())
    todd("pull", "1", "-y")
    assert saved().state == State.DONE


def test_nothing_new_means_no_claude(shell):
    filed(shell)
    asked = len(shell.prompts)
    out = todd("pull").output
    assert "Nothing new on its links." in out and "Everything's up to date." in out
    assert len(shell.prompts) == asked


def test_up_to_date_when_claude_proposes_nothing(shell, picks):
    filed(shell)
    shell.answers.append(proposal(state="todo", next_action=saved().next_action, reason=None))
    out = todd("pull", "1").output  # asked about #1 directly, so Claude looks even with nothing new
    assert "#1 is up to date." in out
    assert all(question != "Update it?" for question, _, _ in picks.asked)


def test_links_only(shell):
    filed(shell)
    ticket_moves_to(shell, "Blocked")
    asked = len(shell.prompts)
    todd("pull", "--links-only")
    assert saved().links[1].status == "Blocked"
    assert len(shell.prompts) == asked


def test_waiting_on_changes_on_their_own(shell, picks):
    filed(shell)
    todd("wait", "1", "Priya", "--local")
    ticket_moves_to(shell, "Blocked")
    shell.answers.append(
        proposal(state="waiting", next_action=saved().next_action, waiting_on="legal sign-off")
    )
    todd("pull", "1")
    task = saved()
    assert (task.state, task.waiting_on) == (State.WAITING, "legal sign-off")


def test_push_moves_jira_to_match(shell):
    filed(shell)
    todd("start", "1", "--local")
    ticket_moves_to(shell, "To Do")
    todd("push", "1", "-y")
    assert shell.transitions == [("PLAT-412", "In Progress")]
    shell.answers.append(answer(links=[]))
    todd("add", "no tickets here", "-y")
    assert "#2 has no Jira ticket to push to" in todd("push", "2").output


def test_pulling_a_project_pulls_its_tasks(shell, picks):
    from .test_projects import a_task

    shell.answers.append(
        answer(title="Ship it", links=[], tasks=[a_task("One", [1], []), a_task("Two", [], [1])])
    )
    todd("add", "one then two", "PLAT-412", "-y")
    ticket_moves_to(shell, "Done")
    shell.answers.append(proposal(reason="One's ticket is Done"))
    out = todd("pull", "1").output
    assert "#1 Ship it" in out and "#2 One" in out and "#3 Two" in out
    assert named("One").state == State.DONE
    assert "Unblocked: #3 Two" in out
