"""Pull requests (and whole stacks of them) and issues, through the GitHub CLI (`gh`).

A pull request in one of GitHub's native stacks is treated as part of one piece of work: todd
reads the stack's membership (REST: GET /repos/{owner}/{repo}/stacks?pull_request=N), then every
member's description, reviews, open review threads, conversation and checks in one GraphQL query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from todd import proc
from todd.errors import ToddError
from todd.links import GitHubItem, github_item
from todd.models import Reviewer, ReviewState

STACKS_API_VERSION = "2026-03-10"
BODY_LIMIT = 2500
REVIEW_LIMIT = 800
COMMENT_LIMIT = 500


@dataclass(frozen=True, slots=True)
class Review:
    author: str | None
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
    body: str


@dataclass(frozen=True, slots=True)
class Remark:
    """A conversation comment, or the opening comment of an unresolved review thread."""

    author: str | None
    body: str
    path: str | None = None


@dataclass(slots=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str | None = None
    url: str | None = None
    state: str | None = None  # open, draft, merged, closed
    author: str | None = None
    body: str | None = None
    base: str | None = None
    head: str | None = None
    review_decision: str | None = None  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED
    checks: str | None = None  # SUCCESS, FAILURE, PENDING, ERROR, EXPECTED
    latest_reviews: list[Review] = field(default_factory=list)  # each reviewer's latest say
    reviews: list[Review] = field(default_factory=list)  # reviews that said something
    requested: list[str] = field(default_factory=list)  # reviewers (logins, org/team slugs)
    open_threads: list[Remark] = field(default_factory=list)
    comments: list[Remark] = field(default_factory=list)
    stack_position: int | None = None  # 1 is the pull request nearest the trunk
    viewer: str | None = None  # the GitHub login of the person using todd

    @property
    def ref(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def reviewers(self) -> list[Reviewer]:
        """Everyone asked to review, or who has: a pending request beats an earlier review."""
        states = {
            "APPROVED": ReviewState.APPROVED,
            "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
            "COMMENTED": ReviewState.COMMENTED,
            "DISMISSED": ReviewState.DISMISSED,
        }
        found: dict[str, Reviewer] = {}

        def add(login: str, state: ReviewState) -> None:
            you = self.viewer is not None and login.lower() == self.viewer.lower()
            found[login.lower()] = Reviewer(login, state, team="/" in login, you=you)

        for review in self.latest_reviews:
            if review.author and review.author != self.author and review.state in states:
                add(review.author, states[review.state])
        for login in self.requested:
            add(login, ReviewState.REQUESTED)
        return list(found.values())

    @property
    def status(self) -> str:
        """A few words on where it stands, like "open · changes requested · checks failing"."""
        if self.state != "open":
            return self.state or "unknown"
        parts = ["open"]
        decision = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes requested",
            "REVIEW_REQUIRED": "needs review",
        }.get(self.review_decision or "")
        if decision:
            parts.append(decision)
        checks = {
            "FAILURE": "checks failing",
            "ERROR": "checks failing",
            "PENDING": "checks running",
            "EXPECTED": "checks running",
        }.get(self.checks or "")
        if checks:
            parts.append(checks)
        return " · ".join(parts)


@dataclass(slots=True)
class Stack:
    owner: str
    repo: str
    number: int
    trunk: str | None
    open: bool
    numbers: list[int]  # bottom (nearest the trunk) to top

    @property
    def key(self) -> str:
        """How todd refers to a stack: owner/repo/stacks/N."""
        return f"{self.owner}/{self.repo}/stacks/{self.number}"


@dataclass(frozen=True, slots=True)
class Issue:
    ref: str
    title: str | None
    state: str | None
    author: str | None
    body: str | None


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _gh(
    argv: list[str], *, what: str, command: str = "gh", timeout: float = 30, partial: bool = False
) -> Any:
    """Run gh and parse its JSON. With `partial`, a GraphQL answer that has data but also
    errors (gh exits non-zero for those) is still returned."""
    try:
        result = proc.run([command, *argv], timeout=timeout)
    except proc.ProcError as e:
        hint = "Install the GitHub CLI and run [bold]gh auth login[/]." if e.missing else None
        raise ToddError(str(e), hint=hint) from e
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        data = None
    if result.ok and data is not None:
        return data
    if partial and isinstance(data, dict) and data.get("data"):
        return data
    if not result.ok:
        raise ToddError(f"Couldn't read {what} from GitHub.", detail=result.complaint)
    raise ToddError(f"gh didn't return JSON for {what}.")


# ── Stacks ───────────────────────────────────────────────────────────────────


def parse_stack(data: Any, owner: str, repo: str) -> Stack | None:
    """Read the stacks list (filtered to one pull request): empty means it isn't stacked."""
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not isinstance(data.get("number"), int):
        return None
    numbers = [
        pr["number"]
        for pr in data.get("pull_requests") or []
        if isinstance(pr, dict) and isinstance(pr.get("number"), int)
    ]
    return Stack(
        owner=owner,
        repo=repo,
        number=data["number"],
        trunk=(data.get("base") or {}).get("ref"),
        open=bool(data.get("open", True)),
        numbers=numbers,
    )


def stack_of(item: GitHubItem, *, command: str = "gh") -> Stack | None:
    """The native stack a pull request belongs to, if any."""
    data = _gh(
        [
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {STACKS_API_VERSION}",
            f"repos/{item.owner}/{item.repo}/stacks?pull_request={item.number}",
        ],
        what=f"the stack for {item.ref}",
        command=command,
    )
    return parse_stack(data, item.owner, item.repo)


# ── Pull requests ────────────────────────────────────────────────────────────

PR_FIELDS = """
fragment PR on PullRequest {
  number title url state isDraft body baseRefName headRefName reviewDecision
  author { login }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  latestReviews(first: 30) { nodes { author { login } state body } }
  reviews(last: 20) { nodes { author { login } state body } }
  reviewRequests(first: 20) {
    nodes { requestedReviewer { __typename ... on User { login } ... on Team { combinedSlug } } }
  }
  reviewThreads(last: 50) {
    nodes { isResolved comments(first: 1) { nodes { author { login } body path } } }
  }
  comments(last: 8) { nodes { author { login } body } }
}
"""


def pulls_query(numbers: list[int]) -> str:
    fields = "\n".join(f"    pr{n}: pullRequest(number: {int(n)}) {{ ...PR }}" for n in numbers)
    return (
        "query Pulls($owner: String!, $name: String!) {\n"
        "  viewer { login }\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}\n" + PR_FIELDS
    )


def _login(node: Any) -> str | None:
    author = (node or {}).get("author") if isinstance(node, dict) else None
    return author.get("login") if isinstance(author, dict) else None


def _nodes(node: dict, name: str) -> list[dict]:
    return [n for n in ((node.get(name) or {}).get("nodes") or []) if isinstance(n, dict)]


def parse_pull(node: dict, owner: str, repo: str) -> PullRequest:
    state = (node.get("state") or "").lower() or None
    if state == "open" and node.get("isDraft"):
        state = "draft"
    commits = _nodes(node, "commits")
    rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}

    def review(n: dict) -> Review:
        return Review(_login(n), n.get("state") or "", _clip(n.get("body"), REVIEW_LIMIT))

    requested = []
    for n in _nodes(node, "reviewRequests"):
        who = n.get("requestedReviewer") or {}
        if name := who.get("login") or who.get("combinedSlug"):
            requested.append(name)
    threads = []
    for thread in _nodes(node, "reviewThreads"):
        if thread.get("isResolved"):
            continue
        first = _nodes(thread, "comments")
        if first:
            c = first[0]
            threads.append(Remark(_login(c), _clip(c.get("body"), COMMENT_LIMIT), c.get("path")))
    return PullRequest(
        owner=owner,
        repo=repo,
        number=int(node.get("number") or 0),
        title=node.get("title"),
        url=node.get("url"),
        state=state,
        author=_login(node),
        body=_clip(node.get("body"), BODY_LIMIT) or None,
        base=node.get("baseRefName"),
        head=node.get("headRefName"),
        review_decision=node.get("reviewDecision"),
        checks=rollup.get("state"),
        latest_reviews=[review(n) for n in _nodes(node, "latestReviews")],
        reviews=[review(n) for n in _nodes(node, "reviews") if (n.get("body") or "").strip()],
        requested=requested,
        open_threads=threads,
        comments=[
            Remark(_login(n), _clip(n.get("body"), COMMENT_LIMIT))
            for n in _nodes(node, "comments")
            if (n.get("body") or "").strip()
        ],
    )


def pulls(owner: str, repo: str, numbers: list[int], *, command: str = "gh") -> list[PullRequest]:
    """Several pull requests from one repository, in one request, in the order asked."""
    data = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={pulls_query(numbers)}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={repo}",
        ],
        what=f"{owner}/{repo} pull requests",
        command=command,
        timeout=60,
        partial=True,
    )
    answer = (data or {}).get("data") or {}
    repository = answer.get("repository") or {}
    viewer = (answer.get("viewer") or {}).get("login")
    found = []
    for n in numbers:
        node = repository.get(f"pr{n}")
        if isinstance(node, dict):
            pr = parse_pull(node, owner, repo)
            pr.viewer = viewer
            found.append(pr)
    if not found:
        errors = "; ".join(e.get("message", "") for e in (data or {}).get("errors") or [])
        raise ToddError(f"GitHub didn't return {owner}/{repo}#{numbers[0]}.", detail=errors or None)
    return found


@dataclass(slots=True)
class Context:
    """A pull request and, when it's stacked, the rest of its stack (bottom to top)."""

    pulls: list[PullRequest]
    stack: Stack | None = None
    stack_error: ToddError | None = None


def pull_request(url: str, *, command: str = "gh") -> Context:
    item = github_item(url)
    if item is None or not item.is_pr:
        raise ToddError(f"{url} isn't a GitHub pull request.")
    stack, stack_error = None, None
    try:
        stack = stack_of(item, command=command)
    except ToddError as e:
        stack_error = e  # older GitHub Enterprise, or the preview API changed: read it alone
    numbers = stack.numbers if stack and item.number in stack.numbers else [item.number]
    found = pulls(item.owner, item.repo, numbers, command=command)
    if stack:
        for pr in found:
            pr.stack_position = stack.numbers.index(pr.number) + 1
    return Context(found, stack, stack_error)


# ── Issues ───────────────────────────────────────────────────────────────────


def issue(url: str, *, command: str = "gh") -> Issue:
    item = github_item(url)
    if item is None:
        raise ToddError(f"{url} isn't a GitHub issue.")
    data = _gh(
        ["issue", "view", url, "--json", "title,state,author,body"], what=item.ref, command=command
    )
    return Issue(
        ref=item.ref,
        title=data.get("title"),
        state=(data.get("state") or "").lower() or None,
        author=(data.get("author") or {}).get("login"),
        body=_clip(data.get("body"), BODY_LIMIT) or None,
    )
