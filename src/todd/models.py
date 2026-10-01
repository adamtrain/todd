"""The things todd keeps track of: tasks, the links attached to them, and their history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class State(StrEnum):
    INBOX = "inbox"  # captured, not yet filed by Claude
    TODO = "todo"
    DOING = "doing"
    WAITING = "waiting"
    IN_REVIEW = "in_review"
    DONE = "done"
    DROPPED = "dropped"
    FOLLOWING = "following"  # not yours to do (yet): something to keep an eye on

    @property
    def label(self) -> str:
        return STATE_LABELS[self]

    @property
    def closed(self) -> bool:
        return self in (State.DONE, State.DROPPED)

    @property
    def progress(self) -> int:
        """How far along the work is, for deciding when follow-ups fire or stop mattering."""
        return PROGRESS[self]


STATE_LABELS = {
    State.INBOX: "inbox",
    State.TODO: "to do",
    State.DOING: "doing",
    State.WAITING: "waiting",
    State.IN_REVIEW: "in review",
    State.DONE: "done",
    State.DROPPED: "dropped",
    State.FOLLOWING: "following",
}

PROGRESS = {
    State.INBOX: 0,
    State.TODO: 0,
    State.FOLLOWING: 0,
    State.DOING: 1,
    State.WAITING: 1,
    State.IN_REVIEW: 2,
    State.DONE: 3,
    State.DROPPED: 3,
}


# Where a following item can go: you take it on, it's over, or you stop following it.
FROM_FOLLOWING = (State.TODO, State.DONE, State.DROPPED)


def parse_state(text: str) -> State | None:
    """A state from how people type it: in_review, in-review, "in review", review…"""
    key = text.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"to_do": "todo", "review": "in_review", "follow": "following", "wait": "waiting"}
    try:
        return State(aliases.get(key, key))
    except ValueError:
        return None


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return list(Priority).index(self)


class LinkKind(StrEnum):
    JIRA = "jira"
    SLACK = "slack"
    GITHUB = "github"
    URL = "url"


class Role(StrEnum):
    """What a link is for, as far as this task is concerned."""

    RESPOND = "respond"  # where you'll need to reply or report back
    SOURCE = "source"  # where the ask came from
    TICKET = "ticket"  # the Jira ticket that tracks this work
    DELIVERABLE = "deliverable"  # the thing being produced or reviewed
    REFERENCE = "reference"  # background

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


ROLE_LABELS = {
    Role.RESPOND: "reply here",
    Role.SOURCE: "where it came from",
    Role.TICKET: "ticket",
    Role.DELIVERABLE: "deliverable",
    Role.REFERENCE: "reference",
}


class EntryKind(StrEnum):
    NOTE = "note"
    STATE = "state"
    TRIAGE = "triage"
    JIRA = "jira"
    SLACK = "slack"
    FOLLOWUP = "followup"


class ReviewState(StrEnum):
    """Where one reviewer stands on one pull request."""

    REQUESTED = "requested"  # asked to review and hasn't yet (or was asked again)
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass(slots=True)
class Reviewer:
    login: str  # a GitHub login, or org/team for a team
    state: ReviewState
    team: bool = False
    you: bool = False  # the person using todd


class FollowupStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"  # no longer needed


@dataclass(slots=True)
class Followup:
    """Something you owe someone about a task: tell them, ask them, check in with them.

    It comes due on a date, or when the task reaches a state (`when`). A dated one can name
    a state (`unless`) that makes it unnecessary: warning Mike that review will slip doesn't
    matter once the doc is out for review.
    """

    action: str
    person: str | None = None
    due: date | None = None
    when: State | None = None
    unless: State | None = None
    status: FollowupStatus = FollowupStatus.OPEN
    by_you: bool = False  # added by you rather than by Claude
    task_id: int | None = None
    id: int | None = None
    created_at: datetime | None = None
    closed_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.status == FollowupStatus.OPEN

    def is_due(self, today: date) -> bool:
        return self.open and self.due is not None and self.due <= today


@dataclass(slots=True)
class Link:
    """A URL (or bare Jira key) attached to a task, with whatever todd knows about it."""

    kind: LinkKind
    url: str | None
    ref: str | None = None  # PROJ-123, owner/repo#12, or a Slack channel/timestamp
    quote: str | None = None  # text you supplied for it, such as the Slack message itself
    author: str | None = None  # who wrote the quote (or the pull request), if known
    role: Role | None = None
    note: str | None = None  # a few words on what this link is
    title: str | None = None  # fetched: Jira summary, PR title
    status: str | None = None  # fetched: Jira status, PR state
    fetched_at: datetime | None = None
    stack: str | None = None  # owner/repo/stacks/N for a pull request in a native GitHub stack
    stack_position: int | None = None  # 1 is the pull request nearest the trunk
    role_fixed: bool = False  # you set the role, so refiling leaves it alone
    reviewers: list[Reviewer] = field(default_factory=list)  # pull requests only
    id: int | None = None
    position: int = 0

    @property
    def target(self) -> str:
        return self.url or self.ref or ""


@dataclass(slots=True)
class Entry:
    kind: EntryKind
    text: str
    at: datetime
    id: int | None = None


# How a project stands while any of its tasks is open: the first of these that one of its
# unblocked open tasks is in. Something moving beats something in review, which beats
# something to do, which beats waiting on others.
STANDING_ORDER = [
    State.DOING,
    State.IN_REVIEW,
    State.TODO,
    State.INBOX,
    State.WAITING,
]


@dataclass(frozen=True, slots=True)
class Standing:
    """Where a project stands, worked out from its tasks."""

    label: str  # "doing", "waiting", "blocked", "deferred", "done"…
    state: State | None  # the state it reads as, if any (for colors)
    until: date | None = None  # for a deferred project: when its first task comes back


def standing(project: Task, tasks: list[Task], today: date) -> Standing:
    """A project's state, worked out from its tasks.

    While any task is open, it's the most active state among the ones that can be worked on
    (not blocked, not deferred). Failing that it's deferred, until the soonest date a deferred
    task comes back, or "blocked" when every open task waits on another. When none is open,
    it's done (or dropped, if every task was). Dropping the project itself is the one thing
    that overrides this.
    """
    if project.state == State.DROPPED:
        return Standing(State.DROPPED.label, State.DROPPED)
    if not tasks:
        return Standing("no tasks yet", None)
    open_tasks = [t for t in tasks if not t.state.closed]
    if not open_tasks:
        finished = State.DONE if any(t.state == State.DONE for t in tasks) else State.DROPPED
        return Standing(finished.label, finished)
    unblocked = [t for t in open_tasks if not t.blocked]
    ready = [t for t in unblocked if not t.deferred(today)]
    for state in STANDING_ORDER:
        if any(t.state == state for t in ready):
            shown = State.TODO if state == State.INBOX else state
            return Standing(shown.label, shown)
    if unblocked:
        return Standing("deferred", None, min(t.defer_until for t in unblocked if t.defer_until))
    return Standing("blocked", None)


@dataclass(frozen=True, slots=True)
class TaskRef:
    """Another task, as far as a task needs to know it: its project, or what blocks it."""

    id: int
    title: str
    state: State


@dataclass(slots=True)
class Task:
    """A task, or a project: a piece of work moved forward through tasks of its own."""

    title: str
    description: str = ""
    state: State = State.INBOX
    next_action: str | None = None
    area: str | None = None  # the area of work it belongs to: platform, hiring…
    is_project: bool = False
    project_id: int | None = None  # the project this task is part of
    project_position: int | None = None  # its place among the project's tasks
    project: TaskRef | None = None
    blockers: list[TaskRef] = field(default_factory=list)  # tasks that must be done first
    priority: Priority = Priority.NORMAL
    due: date | None = None
    due_hint: str | None = None
    defer_until: date | None = None  # not before this date: until then it's out of `todd now`
    waiting_on: str | None = None
    needs_title: bool = False  # Claude couldn't tell what this is; the title is a placeholder
    people: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    followups: list[Followup] = field(default_factory=list)
    triaged_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    state_at: datetime | None = None
    id: int | None = None

    @property
    def triaged(self) -> bool:
        return self.triaged_at is not None

    def links_of(self, kind: LinkKind) -> list[Link]:
        return [link for link in self.links if link.kind == kind]

    @property
    def open_followups(self) -> list[Followup]:
        return [f for f in self.followups if f.open]

    @property
    def open_blockers(self) -> list[TaskRef]:
        """What still has to happen first. A dropped blocker is out of the way too."""
        return [b for b in self.blockers if not b.state.closed]

    @property
    def blocked(self) -> bool:
        return bool(self.open_blockers)

    def deferred(self, today: date) -> bool:
        """Put off until a date that hasn't come yet. (On the date itself, it's back.)"""
        return self.defer_until is not None and self.defer_until > today and not self.state.closed
