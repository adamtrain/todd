"""The six kinds of capture todd was designed around, end to end, with Claude's answers faked.

1. Waiting on review of a PR stack that's tied to tickets.
2. A ticket due Friday, with Slack threads that are only context.
3. An RFC, with a promise to ask Mike to review it and to warn him if it slips.
4. Something to follow, not do.
5. A ticket to finish, then tell Theo.
6. A Slack link todd can't read, so Claude can't say what it is.
"""

import copy
from datetime import date

import pytest

from todd import ask, cli
from todd.models import FollowupStatus, Role, State

from .conftest import PR_URL, SLACK_DM, SLACK_THREAD, Picks, answer, load
from .test_cli import saved, todd


def link_roles(*roles: str | None) -> list[dict]:
    return [
        {"index": i, "role": role, "note": None, "author": None} for i, role in enumerate(roles, 1)
    ]


def followup(action: str, **kw) -> dict:
    return {"action": action, "person": None, "when": None, "due": None, "unless": None, **kw}


def user(login: str) -> dict:
    return {"requestedReviewer": {"__typename": "User", "login": login}}


@pytest.fixture
def two_tickets(shell):
    other = copy.deepcopy(load("acli_view_PLAT-412"))
    other["key"] = "PLAT-413"
    other["fields"]["summary"] = "Cut invoices over to cluster-b"
    shell.tickets["PLAT-413"] = other
    return shell


# ── 1 ──────────────────────────────────────────────────────────────────────


def test_1_waiting_on_review_of_a_stack_tied_to_tickets(two_tickets):
    shell = two_tickets
    shell.pulls["88"]["reviewRequests"] = {"nodes": [user("sam-k"), user("luke-p")]}
    todd("nick", "acme/platform-reviewers", "Platform reviewers")
    shell.answers.append(
        answer(
            title="Land Priya's cluster-b stack",
            track="waiting",
            waiting_on="reviews on the stack",
            links=link_roles("deliverable", "deliverable", "deliverable", "ticket", "ticket"),
        )
    )
    result = todd(
        "add",
        "Currently waiting on review from this PR stack",
        PR_URL,
        "related to these tickets",
        "PLAT-412",
        "PLAT-413",
    )
    assert result.exit_code == 0, result.output
    task = saved()
    assert task.state == State.WAITING
    assert [link.ref for link in task.links] == [
        "acme/billing#85",
        "acme/billing#86",
        "acme/billing#88",
        "PLAT-412",
        "PLAT-413",
    ]

    prompt = shell.prompts[0]
    assert "The person's own GitHub login is @adamtrain." in prompt
    assert "Review requested and not yet given by:" in prompt
    assert "Platform reviewers (GitHub @acme/platform-reviewers) (#86)" in prompt
    assert "Changes requested by: GitHub @sam-k (#86)." in prompt
    assert f"this PR stack {PR_URL} related to these tickets PLAT-412 PLAT-413" in prompt

    # It's waiting, so it isn't in `todd` (what you can act on); `todd ls` has it. Many
    # reviewers are shown compactly there and in full in `todd show`.
    assert "Not yours to act on now: 1 waiting" in todd().output
    listed = " ".join(todd("ls").output.split())  # the reviewers wrap rather than truncate
    assert "waiting on reviews from luke-p, Platform reviewers, sam-k" in listed
    out = todd("show", "1").output
    assert "Waiting on reviews from luke-p, Platform reviewers, sam-k" in out
    assert "#86  open · changes requested  ✗ sam-k  … Platform reviewers  · you" in out
    assert "#88  draft" in out and "… sam-k  … luke-p" in out

    # Both tickets are the work's tickets, so both move when it's done.
    todd("done", "1", "-y")
    assert shell.transitions == [("PLAT-412", "Done"), ("PLAT-413", "Done")]


# ── 2 ──────────────────────────────────────────────────────────────────────


def test_2_a_ticket_with_slack_threads_that_are_only_context(shell):
    shell.answers.append(
        answer(
            title="Spike PLAT-412: can billing workers move to cluster-b?",
            due="2026-10-02",
            due_hint="by Friday",
            links=link_roles("ticket", "reference", "reference"),
        )
    )
    todd("add", "PLAT-412", "by Friday, see also", SLACK_DM, SLACK_THREAD)
    assert "PLAT-412 by Friday, see also https://acme.slack.com" in shell.prompts[0]
    task = saved()
    assert [link.role for link in task.links] == [Role.TICKET, Role.REFERENCE, Role.REFERENCE]

    out = todd("done", "1", "-y").output
    assert "Reply in Slack" not in out  # context isn't somewhere you owe a reply

    # …unless you say it is. Refiling doesn't undo that.
    assert "is reply here on #1" in todd("role", "1", "2", "reply").output
    shell.answers.append(answer(links=link_roles("ticket", "reference", "reference")))
    todd("triage", "1")
    assert saved().links[1].role == Role.RESPOND
    todd("reopen", "1", "--local")
    out = todd("done", "1", "-y").output
    assert "Reply in Slack" in out and SLACK_DM in out and SLACK_THREAD not in out


def test_2b_you_can_say_where_to_reply_when_you_capture(shell):
    shell.answers.append(answer(links=link_roles("ticket", "reference")))
    todd("add", "PLAT-412", "by Friday", "--reply-in", SLACK_DM)
    link = saved().links[1]
    assert (link.url, link.role, link.role_fixed) == (SLACK_DM, Role.RESPOND, True)


# ── 3 ──────────────────────────────────────────────────────────────────────


def test_3_an_rfc_with_promises_to_mike(shell, monkeypatch):
    shell.answers.append(
        answer(
            title="Write the RFC on topic X",
            due="2026-10-02",
            links=[],
            follow_ups=[
                followup("Ask Mike R to review the RFC", person="Mike R", when="in_review"),
                followup(
                    "Tell Mike R the RFC review slips to next week",
                    person="Mike R",
                    due="2026-10-02",
                    unless="in_review",
                ),
            ],
        )
    )
    out = todd(
        "add",
        "I need to write an RFC about topic X and told Mike R I would ask him to review it this "
        "week, but I'm not sure I'll have time this week so it may go into next",
    ).output
    assert "↪1 Ask Mike R to review the RFC  when in review" in out
    assert (
        "↪2 Tell Mike R the RFC review slips to next week  Fri Oct 2, unless it's in review" in out
    )

    # Nothing's due yet; on Friday the warning is.
    assert "Follow-ups due" not in todd().output
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 10, 2))
    out = todd().output
    assert "Follow-ups due 1" in out and "Tell Mike R the RFC review slips" in out
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 30))

    # Getting it into review makes the warning moot and brings up the ask.
    out = todd("review", "1").output
    assert "No longer needed: Tell Mike R the RFC review slips to next week" in out
    assert "Follow-up due: Ask Mike R to review the RFC" in out
    assert "Kept for today: it's in todd now as ↪1" in out
    assert "↪1" in todd().output
    todd("followup", "done", "1")
    statuses = {f.action: f.status for f in saved().followups}
    assert statuses == {
        "Ask Mike R to review the RFC": FollowupStatus.DONE,
        "Tell Mike R the RFC review slips to next week": FollowupStatus.DROPPED,
    }
    assert "Follow-ups due" not in todd().output


# ── 4 ──────────────────────────────────────────────────────────────────────


def test_4_following_something_that_might_land_on_you(shell, monkeypatch):
    shell.answers.append(
        answer(
            title="Follow the ledger write-split refactor",
            track="following",
            links=link_roles("source"),
            follow_ups=[
                followup(
                    "Check in with Dana on the ledger refactor", person="Dana", due="2026-10-14"
                )
            ],
        )
    )
    todd(
        "add",
        "Following",
        SLACK_THREAD,
        "which is related to a refactor of the ledger that might end up on my plate",
        "-q",
        "Heads up: we're splitting ledger writes out next quarter. Dana is scoping it.",
    )
    assert saved().state == State.FOLLOWING
    out = todd().output
    assert "Nothing to act on right now." in out and "1 following" in out
    assert "Follow the ledger" not in todd("ls").output  # only when you ask
    out = todd("ls", "--following").output
    assert "▸ Following 1" in out and "Follow the ledger write-split refactor" in out
    out = todd("following").output
    assert "Follow the ledger write-split refactor" in out
    assert "↪ Check in with Dana on the ledger refactor  Oct 14" in out

    # The check-in shows up in `todd` when it's due, though the item itself doesn't.
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 10, 14))
    out = todd().output
    assert "Follow-ups due 1" in out and "#1 Follow the ledger write-split refactor" in out
    todd("followup", "snooze", "1", "+14")
    assert "Follow-ups due" not in todd().output

    # It isn't yours, so it can't be started or parked: only taken on, finished or dropped.
    for verb in ("start", "wait", "review"):
        result = todd(verb, "1")
        assert result.exit_code == 1
        assert "it can only become to do, done or dropped" in result.output

    # It landed on you after all.
    todd("reopen", "1")
    assert saved().state == State.TODO
    assert "Follow the ledger write-split refactor" in todd().output


# ── 5 ──────────────────────────────────────────────────────────────────────


def test_5_finish_a_ticket_then_tell_theo(shell, clipboard, monkeypatch):
    shell.answers.append(
        answer(
            title="Add a retry budget to the billing webhook consumer (PLAT-412)",
            links=link_roles("ticket"),
            follow_ups=[
                followup("Tell Theo A that PLAT-412 is done", person="Theo A", when="done")
            ],
        )
    )
    todd("add", "I need to complete", "PLAT-412", "and tell Theo A when I'm done")
    assert "Tell Theo A that PLAT-412 is done  when done" in todd("show", "1").output

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(ask, "choose", Picks(["yes", "draft", "yes"]))  # move it, draft, mark done
    shell.answers.append("Theo, PLAT-412 is done: the retry budget is live.")
    result = todd("done", "1", "shipped")
    assert result.exit_code == 0, result.output
    assert shell.transitions == [("PLAT-412", "Done")]
    assert clipboard == ["Theo, PLAT-412 is done: the retry budget is live."]
    assert "What the person promised: Tell Theo A that PLAT-412 is done" in shell.prompts[-1]
    assert "shipped" in shell.prompts[-1]
    assert saved().followups[0].status == FollowupStatus.DONE


# ── 6 ──────────────────────────────────────────────────────────────────────


def mary() -> dict:
    return answer(
        title=None,
        needs_title=True,
        next_action="Reread Mary S's DM to see what she needs",
        links=link_roles("respond"),
        follow_ups=[followup("Tell Mary S it's done", person="Mary S", when="done")],
    )


def test_6_claude_says_when_it_cant_tell_what_something_is(shell):
    shell.answers.append(mary())
    out = todd("add", "I need to address", SLACK_DM, "and tell Mary S when I'm done").output
    assert "Claude couldn't tell what this is" in out
    task = saved()
    assert task.needs_title
    assert task.title == "I need to address Slack DM · Sep 30 and tell Mary S when I'm done"
    assert [f.action for f in task.followups] == ["Tell Mary S it's done"]
    assert 'needs a title · todd edit 1 -t "…"' in todd().output
    todd("edit", "1", "-t", "Fix the export Mary S reported")
    assert not saved().needs_title
    assert "needs a title" not in todd().output


def test_6b_asked_for_a_title_right_away_in_a_terminal(shell, picks):
    shell.answers.append(mary())
    # Enter skips pasting the Slack message; then the title.
    result = todd(
        "add",
        "I need to address",
        SLACK_DM,
        "and tell Mary S when I'm done",
        input="\nFix the export Mary S reported\n",
    )
    assert "What should it be called?" in result.output
    task = saved()
    assert (task.title, task.needs_title) == ("Fix the export Mary S reported", False)


# ── Moving anywhere, and getting at links ─────────────────────────────────


def test_any_state_to_any_other(shell):
    shell.answers.append(answer(links=link_roles("respond", "ticket")))
    todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", "numbers?")
    assert "to do → waiting" in todd("move", "1", "waiting", "-J").output
    assert "waiting → inbox" in todd("move", "1", "inbox").output
    assert "inbox → following" in todd("move", "1", "following").output
    assert "following → to do" in todd("move", "1", "to do", "-J").output
    assert "to do → in review" in todd("move", "1", "in-review", "--no-jira").output
    assert shell.transitions == []
    assert "Left Jira alone" in todd("move", "1", "done", "-J").output
    result = todd("move", "1", "sideways")
    assert result.exit_code == 1 and "isn't a state" in result.output
    todd("move", "1", "doing", "-y")
    assert shell.transitions == []  # PLAT-412 is already In Progress


def test_a_failed_jira_move_gives_you_the_link(shell):
    shell.answers.append(answer(links=link_roles("respond", "ticket")))
    todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", "numbers?")
    shell.transition_error = "✗ Error: no transition to 'Done' is available"
    result = todd("done", "1", "-y")
    assert "Couldn't move PLAT-412 to Done." in result.output
    assert "Move PLAT-412 to Done yourself:" in result.output
    # On a line of its own, with nothing around it, so the terminal can spot it.
    assert "\nhttps://acme.atlassian.net/browse/PLAT-412\n" in result.output


def test_links_prints_bare_links(shell):
    shell.answers.append(answer(links=link_roles("deliverable", "deliverable", "deliverable")))
    todd("add", "review priya's stack", PR_URL)
    result = todd("links", "1")
    assert result.stdout == (
        "https://github.com/acme/billing/pull/85\n"
        "https://github.com/acme/billing/pull/86\n"
        "https://github.com/acme/billing/pull/88\n"
    )
    labelled = todd("links", "1", "--labels").stdout.splitlines()
    assert labelled[0] == "# #1 acme/billing#85: Split billing worker config per cluster"
    assert labelled[1] == "https://github.com/acme/billing/pull/85"


def test_pull_links_only_rereads_without_claude(shell):
    shell.answers.append(answer(links=link_roles("respond", "ticket")))
    todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", "numbers?")
    shell.tickets["PLAT-412"]["fields"]["status"]["name"] = "Blocked"
    calls = len(shell.ran("-p"))
    todd("pull", "--links-only")
    assert saved().links[1].status == "Blocked"
    assert len(shell.ran("-p")) == calls  # no Claude


def test_followup_add_and_drop(shell):
    shell.answers.append(answer(links=link_roles("respond", "ticket")))
    todd("add", "reply to Priya", SLACK_DM, "PLAT-412", "-q", "numbers?")
    out = todd(
        "followup", "add", "1", "Ping", "Sam", "about", "access", "--to", "Sam", "--on", "fri"
    ).output
    assert "↪1 Ping Sam about access  Fri" in out
    assert "Coming up 1" in todd("followup").output
    todd("fu", "drop", "1")
    assert "No follow-ups." in todd("followup").output
    assert saved().followups[0].by_you


def test_adding_and_refiling_never_touch_jira(shell, config_file):
    # Even with every state mapped, only moving a task (or `todd push`) changes a ticket.
    config_file(
        "[jira.status]\n"
        'todo = "To Do"\nwaiting = "Blocked"\nfollowing = "Watching"\n'
        'doing = "In Progress"\ndone = "Done"\n'
    )
    for track in ("todo", "waiting", "following"):
        shell.answers.append(answer(track=track, links=link_roles("ticket")))
        todd("add", f"a {track} task", "PLAT-412")
    shell.answers.append(answer(track="waiting", links=link_roles("ticket")))
    todd("triage", "1")
    todd("link", "1", SLACK_DM)
    todd("pull", "--links-only")
    assert shell.ran("transition") == []
    assert [task.state for task in map(saved, (1, 2, 3))] == [
        State.TODO,
        State.WAITING,
        State.FOLLOWING,
    ]

    todd("move", "2", "done", "-y")  # a state change does move it
    assert shell.transitions == [("PLAT-412", "Done")]
