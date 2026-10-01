"""End to end through the command line, with every outside program faked."""

import json

import pytest
from typer.testing import CliRunner

from todd import cli, db, store
from todd.models import Role, State
from todd.proc import Result

from .conftest import PR_URL, SLACK_DM, SOLO_PR_URL, load

runner = CliRunner()
ASK = "Hey, can you send me the Q3 migration numbers before Thursday's sync?"


def todd(*args: str, input: str | None = None):
    result = runner.invoke(cli.app, list(args), input=input)
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def saved(task_id: int = 1):
    return store.get(db.connect(db.db_path()), task_id)


@pytest.fixture
def filed(shell):
    """One task, captured and filed: a Slack ask plus its Jira ticket."""
    result = todd("add", "reply to Priya about the Q3 numbers", SLACK_DM, "PLAT-412", "-q", ASK)
    assert result.exit_code == 0, result.output
    return shell


def test_empty_queue_says_how_to_start(shell):
    result = todd()
    assert result.exit_code == 0
    assert "Nothing on your list." in result.output
    assert "todd add" in result.output


def test_add_captures_looks_up_and_files(filed):
    task = saved()
    assert task.state == State.TODO
    assert task.title == "Send Priya the Q3 migration numbers"
    assert task.links[0].quote == ASK
    assert task.links[0].role == Role.RESPOND
    assert task.links[1].status == "In Progress"
    # Claude saw the message as the Slack message behind that link.
    assert f"<message>\n{ASK}\n</message>" in filed.prompts[0]


def test_add_shows_what_happened(shell):
    result = todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", ASK)
    out = result.output
    assert "◇ captured · 3 words · 2 links" in out
    assert "message text kept (12 words)" in out
    assert "✓ PLAT-412 · Migrate billing workers to the new cluster · In Progress" in out
    assert "Filed #1 in to do" in out
    assert "Send Priya the Q3 migration numbers" in out
    assert "→ Pull the Q3 numbers from the migration dashboard" in out


def test_queue_groups_by_state(filed):
    todd("add", "--raw", "look at the flaky deploy job")
    result = todd()
    out = result.output
    assert out.index("To do") < out.index("Inbox")
    assert "#1" in out and "Send Priya the Q3 migration numbers" in out
    assert "todd triage 2" in out
    assert "PLAT-412" in out and "Slack" in out
    assert "1 to do · 1 inbox" in out


def test_show_has_the_message_links_and_timeline(filed):
    out = todd("show", "1").output
    assert ASK in out
    assert "— Priya" in out
    assert "reply here" in out
    assert "PLAT-412 · In Progress · ticket" in out
    assert "Timeline" in out and "Filed by Claude" in out


def test_state_changes_move_the_jira_ticket(filed):
    result = todd("start", "1", "-y")
    assert "to do → doing" in result.output
    assert filed.transitions == []  # already In Progress
    result = todd("review", "1", "-y")
    assert filed.transitions == [("PLAT-412", "In Review")]
    assert "PLAT-412 is now In Review" in result.output
    todd("done", "1", "-y", "sent", "them")
    assert filed.transitions[-1] == ("PLAT-412", "Done")
    task = saved()
    assert task.state == State.DONE
    assert task.links[1].status == "Done"
    assert [e.text for e in task.entries][-3:] == [
        "in review → done",
        "sent them",
        "PLAT-412 → Done",
    ]


def test_jira_status_is_rechecked_before_moving(filed):
    filed.tickets["PLAT-412"]["fields"]["status"]["name"] = "In Review"  # someone moved it
    todd("review", "1", "-y")
    assert filed.transitions == []


def test_without_yes_or_a_terminal_jira_is_left_alone(filed):
    result = todd("review", "1")
    assert filed.transitions == []
    assert "Left PLAT-412 alone: todd asks before changing Jira" in result.output
    assert "Pass -y to move it anyway, or run todd sync 1" in result.output
    todd("sync", "1", "-y")
    assert filed.transitions == [("PLAT-412", "In Review")]
    assert "Jira already matches" in todd("sync", "1").output


def test_every_jira_change_is_asked_about_and_no_is_the_default(filed, picks):
    result = todd("review", "1")  # Enter on the default
    assert filed.transitions == []
    assert "Left PLAT-412 alone." in result.output
    question, keys, default = picks.asked[0]
    assert (question, keys, default) == ("Move PLAT-412 to In Review?", ["no", "yes"], "no")
    picks.script.append("yes")
    todd("sync", "1")
    assert filed.transitions == [("PLAT-412", "In Review")]


def test_each_ticket_gets_its_own_question(shell, picks):
    other = dict(load("acli_view_PLAT-412"), key="PLAT-413")
    shell.tickets["PLAT-413"] = other
    shell.answers.append(
        dict(
            load("claude_envelope")["structured_output"],
            links=[
                {"index": 1, "role": "ticket", "note": None, "author": None},
                {"index": 2, "role": "ticket", "note": None, "author": None},
            ],
        )
    )
    todd("add", "two tickets", "PLAT-412", "PLAT-413")
    picks.script += ["yes", "no"]
    todd("done", "1")
    assert shell.transitions == [("PLAT-412", "Done")]
    assert [q for q, _, _ in picks.asked] == ["Move PLAT-412 to Done?", "Move PLAT-413 to Done?"]


def test_yes_and_no_jira_skip_the_question_in_each_direction(filed, picks):
    def jira_questions() -> list[str]:
        return [q for q, _, _ in picks.asked if q.startswith("Move ")]

    todd("review", "1", "--no-jira")
    assert filed.transitions == [] and jira_questions() == []
    todd("done", "1", "-y")
    assert filed.transitions == [("PLAT-412", "Done")] and jira_questions() == []
    result = todd("reopen", "1", "-y", "--no-jira")
    assert result.exit_code == 1
    assert "-y says to change Jira and --no-jira (or --local) says not to" in result.output


def test_local_changes_only_todd(filed):
    todd("done", "1", "--local")
    assert filed.transitions == []


def test_jira_failure_is_reported_but_the_state_change_stands(filed):
    filed.transition_error = "✗ Error: no transition to 'In Review'"
    result = todd("review", "1", "-y")
    assert "Couldn't move PLAT-412 to In Review" in result.output
    assert saved().state == State.IN_REVIEW


def test_done_points_you_at_the_slack_thread(filed):
    out = todd("done", "1", "-y").output
    assert "Reply in Slack" in out
    assert ASK in out
    assert SLACK_DM in out


def test_interactive_done_confirms_jira_then_drafts_a_reply(filed, clipboard, picks):
    filed.answers.append("Sent! They're in the Q3 sheet.")
    picks.script += ["yes", "draft", "yes"]  # move the ticket, draft a reply, open the thread
    result = todd("done", "1", "all sent")
    assert result.exit_code == 0, result.output
    assert filed.transitions == [("PLAT-412", "Done")]
    assert clipboard == ["Sent! They're in the Q3 sheet."]
    assert filed.opened == [SLACK_DM]
    reply_prompt = filed.prompts[-1]
    assert ASK in reply_prompt and "all sent" in reply_prompt
    assert saved().entries[-1].text == "Drafted a Slack reply"


def test_interactive_add_asks_for_the_slack_message(shell, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = todd("add", "reply to Priya", SLACK_DM, input="Hey, numbers?\n\nThanks!\n")
    assert result.exit_code == 0, result.output
    assert "Paste the message" in result.output
    assert saved().links[0].quote == "Hey, numbers?\n\nThanks!"


def test_interactive_add_can_skip_the_message(shell, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    todd("add", "reply to Priya", SLACK_DM, input="\n")
    assert saved().links[0].quote is None


def test_piped_text_uses_the_capture_format(shell, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_piped", lambda stdin: True)
    todd("add", input=f"Reply to Priya\n\n{SLACK_DM}\n{ASK}\n\nPLAT-412\n")
    task = saved()
    assert task.description == "Reply to Priya"
    assert task.links[0].quote == ASK


def test_editor_capture(shell, monkeypatch):
    monkeypatch.setattr(
        cli.system, "edit", lambda text: text.replace("reply to Priya", "reply to Priya today")
    )
    todd("add", "-e", "reply to Priya", SLACK_DM, "-q", ASK)
    task = saved()
    assert task.description == f"reply to Priya today {SLACK_DM}"
    assert task.links[0].quote == ASK


def test_claude_failure_keeps_the_task_in_the_inbox(shell):
    shell.answers.append(Result(1, "", "Invalid API key · Please run /login"))
    result = todd("add", "reply to Priya", SLACK_DM)
    assert result.exit_code == 1
    assert "saved in your inbox, unfiled" in result.output
    assert "todd triage 1" in result.output
    task = saved()
    assert task.state == State.INBOX
    assert task.title == "reply to Priya Slack DM · Sep 30"  # your words, links by name
    result = todd("triage")
    assert result.exit_code == 0, result.output
    assert saved().state == State.TODO


def test_raw_skips_lookups_and_claude(shell):
    result = todd("add", "--raw", "think about the roadmap")
    assert "Saved #1 to your inbox" in result.output
    assert shell.calls == []


def test_duplicate_links_are_flagged(filed):
    result = todd("add", "--raw", "chase PLAT-412", "PLAT-412")
    assert "PLAT-412 is already on #1" in result.output


def test_wait_records_on_what(filed):
    result = todd("wait", "1", "Priya", "to", "confirm", "the", "numbers")
    assert "waiting on Priya to confirm the numbers" in result.output
    assert saved().waiting_on == "Priya to confirm the numbers"
    assert "on Priya to confirm the numbers" in todd().output
    todd("start", "1", "--local")
    assert saved().waiting_on is None


def test_repeating_a_state_is_a_no_op(filed):
    assert "already to do" in todd("reopen", "1").output


def test_edit_corrects_the_filing(filed):
    result = todd("edit", "1", "--due", "fri", "--priority", "urgent", "--project", "none")
    assert result.exit_code == 0, result.output
    task = saved()
    assert task.due is not None and task.due.isoformat() == "2026-10-02"
    assert task.priority.value == "urgent"
    assert task.project is None
    assert task.entries[-1].text == "Edited project, priority, due"


def test_edit_rejects_a_bad_date(filed):
    result = todd("edit", "1", "--due", "someday")
    assert result.exit_code == 1
    assert "isn't a date" in result.output


def test_link_note_open_and_reply(filed):
    result = todd("link", "1", SOLO_PR_URL)
    assert "✓ acme/billing#90 · Bump httpx · open · approved · checks failing" in result.output
    assert "Added 1 link to #1" in result.output
    assert "already on #1" in todd("link", "1", SOLO_PR_URL).output
    todd("note", "1", "Priya", "prefers", "a", "sheet")
    task = saved()
    assert task.links[2].title == "Bump httpx"
    assert task.entries[-1].text == "Priya prefers a sheet"
    todd("open", "1", "3")
    assert filed.opened == [SOLO_PR_URL]
    out = todd("reply", "1").output
    assert "Reply in Slack" in out and SLACK_DM in out


def test_unknown_task(shell):
    result = todd("show", "42")
    assert result.exit_code == 1
    assert "There's no task #42" in result.output


def test_filters(filed):
    assert "#1" in todd("ls", "--project", "platform").output
    assert "Nothing on your list." in todd("ls", "--kind", "review").output
    todd("done", "1", "--local")
    assert "#1" not in todd().output
    assert "#1" in todd("ls", "--all").output


def test_config_shows_and_initializes(shell, config_file, isolated):
    out = todd("config").output
    assert "not created yet" in out
    assert "In Review" in out
    assert todd("config", "--init").exit_code == 0
    assert (isolated / "config.toml").exists()
    config_file(
        '[jira]\nsite = "acme.atlassian.net"\n[jira.projects.OPS.status]\ndone = "Closed"\n'
    )
    out = todd("config").output
    assert "acme.atlassian.net" in out
    assert "OPS: Closed" in out


def test_bad_config_is_explained(shell, config_file):
    config_file("[jira]\nsitee = 'x'\n")
    result = todd("add", "x")
    assert result.exit_code == 1
    assert "Unknown setting in [jira]: sitee" in result.output


def test_doctor(shell):
    shell.answers.append({"ok": True})
    result = todd("doctor", "--claude", "--jira", "plat-412")
    out = result.output
    assert "✓ claude" in out
    assert "structured answers work" in out
    assert "✓ jira auth" in out
    assert "'Migrate billing workers to the new cluster' · In Progress · Story" in out
    assert "description: Move the billing-worker" in out


def test_doctor_explains_a_missing_acli(shell):
    shell.missing.add("acli")
    out = todd("doctor").output
    assert "✗ acli" in out
    assert "developer.atlassian.com/cloud/acli" in out


def test_version():
    assert todd("--version").output.startswith("todd ")


def test_claude_is_run_outside_the_project_and_without_tools(filed):
    call = filed.ran("-p")[0]
    assert call.argv[call.argv.index("--tools") + 1] == ""
    schema = json.loads(call.argv[call.argv.index("--json-schema") + 1])
    assert schema["properties"]["kind"]["enum"][0] == "do"
    assert load("claude_envelope")["structured_output"]["kind"] == "reply"


# ── Pull request stacks and nicknames ───────────────────────────────────────


def test_a_stacked_pull_request_brings_the_whole_stack(shell):
    shell.answers.append(
        dict(
            load("claude_envelope")["structured_output"],
            title="Get Priya's billing cutover stack landed",
            kind="review",
            links=[
                {"index": i, "role": "deliverable", "note": None, "author": None} for i in (1, 2, 3)
            ],
        )
    )
    result = todd("add", "review priya's cluster-b stack", PR_URL)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "+ acme/billing#85 (1 in its stack) · Split billing worker config per cluster" in out
    assert "✓ acme/billing#86 (2 in its stack) · Move billing-worker to cluster-b" in out
    assert "+ acme/billing#88 (3 in its stack) · Cut production over to cluster-b · draft" in out
    task = saved()
    assert sorted(link.ref or "" for link in task.links) == [
        "acme/billing#85",
        "acme/billing#86",
        "acme/billing#88",
    ]
    assert {link.role for link in task.links} == {Role.DELIVERABLE}
    assert "stack of 3" in todd().output


def test_show_groups_a_stack_bottom_to_top(shell):
    todd("nick", "priya-n", "Priya")
    todd("add", "review priya's stack", SLACK_DM, PR_URL, "-q", "can you look at my stack?")
    task = saved()
    # Stack members are numbered together, bottom to top, where the stack was linked.
    assert [link.ref for link in task.links] == [
        "D024BE91L/1790776800.123456",
        "acme/billing#85",
        "acme/billing#86",
        "acme/billing#88",
    ]
    out = todd("show", "1").output
    assert "GitHub stack 17 in acme/billing · 3 pull requests · 1 merged, 1 open, 1 draft" in out
    assert (
        out.index("1/3 acme/billing#85")
        < out.index("2/3 acme/billing#86")
        < out.index("3/3 acme/billing#88")
    )
    assert "Move billing-worker to cluster-b · by Priya" in out


def test_nicknames(shell, isolated):
    assert "No nicknames yet" in todd("nick").output
    todd("nick", "@Priya-N", "Priya", "Nair")
    todd("nick", "sam-k", "Sam")
    out = todd("nick").output
    assert "@priya-n  Priya Nair" in out and "@sam-k  Sam" in out
    assert todd("nick", "sam-k").output.strip() == "Sam"
    todd("nick", "sam-k", "--remove")
    assert "has no nickname" in todd("nick", "sam-k").output
    assert (isolated / "nicknames.toml").read_text() == '"priya-n" = "Priya Nair"\n'


def test_nicknames_reach_claude(shell):
    todd("nick", "sam-k", "Sam")
    todd("add", "review priya's stack", PR_URL)
    assert "Sam (GitHub @sam-k): requested changes" in shell.prompts[0]


def test_person_filter_understands_logins_and_nicknames(shell):
    todd("nick", "priya-n", "Priya")
    todd("add", "review the stack", PR_URL)  # Claude records "Priya"; the PRs are by priya-n
    assert "#1" in todd("ls", "--person", "priya-n").output
    assert "#1" in todd("ls", "--person", "Priya").output
    assert "Nothing on your list." in todd("ls", "--person", "zed").output


def test_doctor_reads_a_stack(shell):
    todd("nick", "sam-k", "Sam")
    out = todd("doctor", "--pr", PR_URL).output
    assert "✓ stack" in out and "#17 in acme/billing · 3 pull requests onto main · open" in out
    assert "2. #86 Move billing-worker to cluster-b · open · changes requested" in out
    assert "reviews: Sam changes requested, you commented" in out
    assert "1 open thread" in out
