"""Recognize what a link points at: a Jira ticket, a Slack message, a GitHub PR, or other."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from todd.models import Link, LinkKind

JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}-[1-9][0-9]*")
_JIRA_KEY_IN_TEXT = re.compile(r"(?<![\w/.-])([A-Z][A-Z0-9_]{1,9})-([1-9][0-9]*)(?![\w-])")
_URL_IN_TEXT = re.compile(r"<?(https?://[^\s<>|]+)")
_SLACK_PERMALINK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
_GITHUB = re.compile(r"^/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)")

# Trailing characters that are almost always punctuation around a URL, not part of it.
_TRAILING = ".,;:!?)]}'\">"


@dataclass(frozen=True, slots=True)
class SlackMessage:
    """What a Slack permalink tells us without asking Slack anything."""

    workspace: str
    channel: str
    ts: str
    thread_ts: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.channel}/{self.ts}"

    @property
    def posted(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), UTC)

    @property
    def in_thread(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts

    @property
    def where(self) -> str:
        match self.channel[:1]:
            case "D":
                return "DM"
            case "G":
                return "private channel or group DM"
        return "channel"


@dataclass(frozen=True, slots=True)
class GitHubItem:
    owner: str
    repo: str
    number: int
    is_pr: bool

    @property
    def ref(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def jira_url(key: str, site: str | None) -> str | None:
    if not site:
        return None
    site = site.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{site}/browse/{key}"


def slack_message(url: str) -> SlackMessage | None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host.endswith(".slack.com"):
        return None
    match = _SLACK_PERMALINK.search(parts.path)
    if not match:
        return None
    channel, seconds, micros = match.groups()
    thread = parse_qs(parts.query).get("thread_ts", [None])[0]
    return SlackMessage(
        workspace=host.removesuffix(".slack.com").removesuffix(".enterprise"),
        channel=channel,
        ts=f"{seconds}.{micros}",
        thread_ts=thread,
    )


def github_item(url: str) -> GitHubItem | None:
    parts = urlsplit(url)
    if parts.hostname != "github.com":
        return None
    match = _GITHUB.match(parts.path)
    if not match:
        return None
    owner, repo, what, number = match.groups()
    return GitHubItem(owner, repo, int(number), what == "pull")


def jira_key_in_url(url: str) -> str | None:
    parts = urlsplit(url)
    if found := re.search(r"/browse/(" + JIRA_KEY.pattern + r")(?:$|[/?#])", parts.path + "/"):
        return found.group(1)
    selected = parse_qs(parts.query).get("selectedIssue", [""])[0]
    if JIRA_KEY.fullmatch(selected):
        return selected
    return None


def from_url(url: str) -> Link:
    """Classify a URL."""
    url = url.strip().strip("<>")
    if message := slack_message(url):
        return Link(LinkKind.SLACK, url, ref=message.ref)
    if (urlsplit(url).hostname or "").endswith(".slack.com"):
        return Link(LinkKind.SLACK, url)
    if key := jira_key_in_url(url):
        return Link(LinkKind.JIRA, url, ref=key)
    if item := github_item(url):
        return Link(LinkKind.GITHUB, url, ref=item.ref)
    return Link(LinkKind.URL, url)


def from_key(key: str, site: str | None) -> Link:
    return Link(LinkKind.JIRA, jira_url(key, site), ref=key)


def recognize(token: str, *, site: str | None = None) -> Link | None:
    """A command-line argument that is, in its entirety, a link or a Jira key."""
    token = token.strip().strip("<>")
    if is_url(token):
        return from_url(token)
    if JIRA_KEY.fullmatch(token):
        return from_key(token, site)
    return None


def _clean_url(url: str) -> str:
    while url and url[-1] in _TRAILING:
        # Keep a closing paren that balances one inside the URL (Wikipedia-style links).
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def in_text(text: str, *, site: str | None = None, keys: Collection[str] = ()) -> list[Link]:
    """Links mentioned inside free text.

    Every URL counts. Bare Jira keys only count for projects listed in `keys`, because
    things like UTF-8 and SHA-256 look just like ticket keys.
    """
    found: list[tuple[int, Link]] = []
    for match in _URL_IN_TEXT.finditer(text):
        if url := _clean_url(match.group(1)):
            found.append((match.start(), from_url(url)))
    if keys:
        wanted = {k.upper() for k in keys}
        for match in _JIRA_KEY_IN_TEXT.finditer(text):
            if match.group(1) in wanted:
                found.append((match.start(), from_key(match.group(0), site)))
    return [link for _, link in sorted(found, key=lambda pair: pair[0])]


def dedupe(links: list[Link]) -> list[Link]:
    """Drop repeats (same ref, else same URL), keeping the first and any quote a repeat carried."""
    seen: dict[str, Link] = {}
    kept: list[Link] = []
    for link in links:
        key = f"{link.kind}:{link.ref or link.url}"
        if key in seen:
            first = seen[key]
            first.quote = first.quote or link.quote
            first.url = first.url or link.url
            continue
        seen[key] = link
        kept.append(link)
    return kept


def short_url(url: str, width: int = 40) -> str:
    parts = urlsplit(url)
    text = (parts.hostname or "").removeprefix("www.") + parts.path.rstrip("/")
    return text if len(text) <= width else text[: width - 1] + "…"


def label(link: Link) -> str:
    """A few words naming a link, for lists."""
    match link.kind:
        case LinkKind.JIRA:
            return link.ref or short_url(link.target)
        case LinkKind.SLACK:
            message = slack_message(link.url) if link.url else None
            if message is None:
                return "Slack"
            posted = message.posted.astimezone()
            thread = " thread" if message.in_thread else ""
            return f"Slack {message.where}{thread} · {posted:%b} {posted.day}"
        case LinkKind.GITHUB:
            return link.ref or short_url(link.target)
    return short_url(link.target)
