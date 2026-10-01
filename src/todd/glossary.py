"""What todd's words mean: states, projects, blocking, following. Shown by `todd states`, and
given to Claude so that what you say maps onto the right state."""

from __future__ import annotations

from todd.models import Role, State

STATES: list[tuple[State, str]] = [
    (State.INBOX, "Captured but not filed by Claude yet. `todd triage` files it."),
    (State.TODO, "Yours to do, not started."),
    (State.DOING, "You're working on it."),
    (
        State.WAITING,
        "You've done your part and are waiting on someone or something, like reviews or an "
        "answer. It says what on (`todd wait 12 Priya to confirm`).",
    ),
    (State.IN_REVIEW, "Your work is done and out for review."),
    (State.DONE, "Finished."),
    (State.DROPPED, "Not doing it after all."),
    (State.FOLLOWING, "Not yours (yet): something you're keeping an eye on. See Following."),
]

BLOCKED = (
    "Not a state of its own: a task is blocked while any task it waits on is still open "
    "(neither done nor dropped). Blocked tasks stay out of `todd now` until they're free. Tasks "
    "can wait on any set of others: one after another, or several at once with the same "
    "blocker, or none. `todd block 16 --on 15`, `todd unblock 16`."
)

DEFERRED = (
    "Not a state either: a task can be put off until a date (`todd defer 12 mon`, "
    "`todd defer 12 none` to stop). Until that date it stays out of `todd now`; `todd ls` still "
    "shows it, marked deferred. On the date it's back. It's separate from a due date, and a "
    "task can have both. Starting or finishing a deferred task ends the deferral."
)

PROJECTS = (
    "A project is work moved forward through one or more tasks. Its state is never set by "
    "hand; it comes from its tasks: doing if any task is under way; otherwise in review, to "
    "do, then waiting, among tasks that aren't blocked or deferred; deferred when every task "
    "that could be worked on is deferred, until the soonest of their dates (a project is never "
    "deferred itself); blocked when every open task waits on another; done when all its tasks "
    "are done (dropped if they were all dropped). Add a task "
    "to a finished project and it's open again. You can drop a whole project, which drops its "
    "open tasks. Tasks outside any project are listed under No project."
)

FOLLOWING = (
    "Following is for things that aren't yours (yet): something to keep an eye on, with a "
    "check-in date. It's always a task of its own, never part of a project, and stays out of "
    "your lists unless you ask (`todd following`, `todd ls --following`). From there you can "
    "only take it on (to do), mark it done (it's over), or drop it (stop following)."
)

NUMBERS = (
    "Numbers are kept as low as they can be. Open things are numbered from 1 with no gaps, so "
    "when you finish or drop something, the ones after it move down to fill its place, and todd "
    "says what moved. What's finished or dropped keeps a number after the open ones, most "
    "recently closed first, so the thing you just closed is the first number after your open "
    "ones (`todd ls --all` shows them; `todd reopen 9` brings one back). Follow-ups (↪) are "
    "numbered the same way."
)

ROLES: list[tuple[Role, str]] = [
    (Role.RESPOND, "where you'll reply or report back"),
    (Role.SOURCE, "where the ask came from"),
    (Role.TICKET, "the Jira ticket that tracks the work (moves when the task does, if you say)"),
    (Role.DELIVERABLE, "the thing being produced or reviewed, like a pull request"),
    (Role.REFERENCE, "background"),
]

FOLLOW_UPS = (
    "A follow-up is something you owe a person: tell them, ask them, check in. It's due on a "
    "date, or when its task reaches a state, and can stop mattering if the task gets "
    "somewhere first. Not to be confused with following."
)


def as_text() -> str:
    """The whole glossary, plainly: for Claude."""
    lines = ["Task states:"]
    lines += [f"- {state.value} ({state.label}): {meaning}" for state, meaning in STATES]
    lines += [f"Blocked: {BLOCKED}", f"Deferred: {DEFERRED}"]
    lines += [f"Projects: {PROJECTS}", f"Following: {FOLLOWING}"]
    lines.append(f"Follow-ups: {FOLLOW_UPS}")
    lines.append(f"Numbers: {NUMBERS}")
    return "\n".join(lines)
