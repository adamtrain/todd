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


def parse_state(text: str) -> State | None:
    """A state from how people type it: in_review, in-review, "in review", review…"""
    key = text.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"to_do": "todo", "review": "in_review", "follow": "following", "wait": "waiting"}
    try:
        return State(aliases.get(key, key))
    except ValueError:
        return None


class Kind(StrEnum):
    DO = "do"  # produce or change something
    REPLY = "reply"  # someone asked you something; you owe an answer
    REVIEW = "review"  # look over someone else's work
    DECIDE = "decide"  # make or drive a decision
    FOLLOW_UP = "follow_up"  # chase someone else for something
    INVESTIGATE = "investigate"  # find something out, debug, research

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


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


@dataclass(slots=True)
class Task:
    title: str
    description: str = ""
    state: State = State.INBOX
    next_action: str | None = None
    kind: Kind | None = None
    project: str | None = None
    priority: Priority = Priority.NORMAL
    due: date | None = None
    due_hint: str | None = None
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
