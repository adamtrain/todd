"""Who is reviewing a task's pull requests: who you wait on, who approved, who wants changes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from todd.models import Link, LinkKind, Reviewer, ReviewState

CLOSED_PR_STATES = ("merged", "closed")


def number(link: Link) -> str:
    return "#" + (link.ref or "?").rsplit("#", 1)[-1]


def is_open_pull(link: Link) -> bool:
    status = (link.status or "").split(" · ")[0]
    return link.kind == LinkKind.GITHUB and status not in CLOSED_PR_STATES


@dataclass(slots=True)
class Board:
    """Reviewers across some pull requests, each with the PRs they're in that state on."""

    waiting: dict[str, list[str]] = field(default_factory=dict)  # asked, and haven't reviewed
    changes: dict[str, list[str]] = field(default_factory=dict)  # requested changes
    approved: dict[str, list[str]] = field(default_factory=dict)
    yours: list[str] = field(default_factory=list)  # PRs where *you* have been asked to review
    teams: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.waiting or self.changes or self.approved or self.yours)


def board(links: Iterable[Link]) -> Board:
    """Tally open pull requests' reviewers. Merged and closed PRs don't hold anything up."""
    result = Board()
    buckets = {
        ReviewState.REQUESTED: result.waiting,
        ReviewState.CHANGES_REQUESTED: result.changes,
        ReviewState.APPROVED: result.approved,
    }
    for link in links:
        if not is_open_pull(link):
            continue
        for reviewer in link.reviewers:
            if reviewer.you:
                if reviewer.state == ReviewState.REQUESTED:
                    result.yours.append(number(link))
                continue
            if reviewer.team:
                result.teams.add(reviewer.login)
            bucket = buckets.get(reviewer.state)
            if bucket is not None:
                bucket.setdefault(reviewer.login, []).append(number(link))
    return result


def ranked(people: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    """Most pull requests first, then alphabetically."""
    return sorted(people.items(), key=lambda item: (-len(item[1]), item[0].lower()))


def reviewers_of(links: Iterable[Link]) -> list[Reviewer]:
    return [r for link in links for r in link.reviewers]
