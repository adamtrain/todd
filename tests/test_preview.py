"""Seeing what Claude would file before it's saved, and asking it to change things."""

from todd import triage
from todd.models import Priority, State

from .conftest import PR_URL, answer
from .test_cli import saved, todd
from .test_projects import a_task


def test_you_see_it_before_it_is_saved(shell, picks):
    result = todd("add", "reply to Priya about the numbers")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Claude would file this · not saved yet" in out
    assert "Send Priya the Q3 migration numbers" in out
    question, keys, default = picks.asked[0]
    assert (question, keys, default) == ("File it?", ["add", "change", "leave"], "add")
    assert saved().state == State.TODO
    assert "Filed #1 in to do" in out


def test_asking_claude_to_change_it(shell, picks):
    shell.answers += [answer(), answer(priority="urgent")]
    picks.script += ["change", "add"]
    result = todd("add", "reply to Priya about the numbers", input="make it urgent\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Claude would file this") == 2
    revised = shell.prompts[1]
    assert "<previous_filing>" in revised and '"priority": "high"' in revised
    assert revised.rstrip().endswith(
        "<change>make it urgent</change>\nReturn the whole filing again with the changes made."
    )
    assert saved().priority == Priority.URGENT


def test_changes_add_up(shell, picks):
    shell.answers += [answer(), answer(), answer()]
    picks.script += ["change", "change", "add"]
    todd("add", "reply to Priya", input="make it urgent\nand due tomorrow\n")
    last = shell.prompts[-1]
    assert "these changes, in order:" in last
    assert last.index("<change>make it urgent</change>") < last.index(
        "<change>and due tomorrow</change>"
    )


def test_enter_goes_back_without_asking_claude(shell, picks):
    picks.script += ["change", "add"]
    todd("add", "reply to Priya", input="\n")
    assert len(shell.prompts) == 1
    assert saved().state == State.TODO


def test_leaving_it_in_the_inbox(shell, picks):
    picks.script.append("leave")
    out = todd("add", "reply to Priya").output
    assert "#1 is in your inbox, unfiled. File it later with todd triage 1" in out
    task = saved()
    assert task.state == State.INBOX and not task.triaged
    assert "Filed by Claude" not in [e.text for e in task.entries]


def test_yes_files_without_a_preview(shell, picks):
    out = todd("add", "reply to Priya", "-y").output
    assert "Claude would file this" not in out
    assert [q for q, _, _ in picks.asked if q == "File it?"] == []
    assert saved().state == State.TODO


def test_a_project_preview_shows_its_tasks_and_where_links_go(shell, picks):
    shell.answers.append(
        answer(
            title="Land cluster-b",
            links=[],
            tasks=[a_task("Get the stack reviewed", [2], []), a_task("Bug bash", [], [1])],
        )
    )
    out = todd("add", "stack then a bug bash", PR_URL).output
    flat = " ".join(out.split())
    assert "PROJECT" in out and "Tasks · 2 tasks" in out
    # Claude named #86; the whole stack goes with that task.
    assert "acme/billing#85 → task 1" in flat and "acme/billing#88 → task 1" in flat
    assert "Bug bash after task 1" in flat
    assert saved(1).is_project


def test_refiling_shows_a_preview_too(shell, picks):
    todd("add", "reply to Priya", "-y")
    shell.answers.append(answer(title="Something else entirely"))
    picks.script.append("leave")
    out = todd("triage", "1").output
    assert picks.asked[-1][1] == ["add", "change", "leave"]
    assert "Left #1 as it was." in out
    assert saved().title == "Send Priya the Q3 migration numbers"


def test_without_a_terminal_it_just_files(shell):
    out = todd("add", "reply to Priya").output
    assert "Claude would file this" not in out
    assert saved().state == State.TODO


def test_the_revision_prompt():
    filing = triage.Filing(title="x", answer={"title": "x", "priority": "low"})
    text = triage.revision(filing, ["make it urgent"])
    assert '"priority": "low"' in text
    assert "asked for this change:\n<change>make it urgent</change>" in text
    assert "the person's own instructions" in triage.SYSTEM
