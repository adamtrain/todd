"""Turning what you typed (arguments, editor text, or piped input) into a description plus links.

The text format, used by the editor and by piped input:

    Reply to Priya with the Q3 migration numbers

    https://acme.slack.com/archives/D024BE91L/p1727712000123456
    Hey, can you send me the Q3 migration numbers before Thursday's sync?

    PROJ-123

Everything before the first link is the description. A line holding nothing but a link (or a
Jira key) starts that link, and the lines under it, up to the next link, are its text: paste a
Slack message there and todd keeps it as that message. `> ` quote markers are optional.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from todd import links as linking
from todd.errors import ToddError
from todd.models import Link, LinkKind

EDITOR_HELP = """\
# Describe the task first, then put each link on its own line.
# Text under a link stays with it as that message: paste a Slack message there.
# Lines starting with # are ignored. Save and quit to capture; leave it empty to cancel.
"""


@dataclass(slots=True)
class Capture:
    description: str = ""
    links: list[Link] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.description.strip() and not self.links


def from_args(
    words: list[str],
    quotes: list[str] | None = None,
    *,
    site: str | None = None,
    keys: Collection[str] = (),
) -> Capture:
    """Arguments that are a link (or a Jira key) are links. The description is everything, as
    typed, links included, so "see also <link>" still says what that link is for."""
    capture = Capture()
    prose: list[str] = []
    for word in words:
        prose.append(word)
        if link := linking.recognize(word, site=site):
            capture.links.append(link)
            continue
        capture.links += linking.in_text(word, site=site, keys=keys)
    capture.description = " ".join(prose).strip()
    capture.links = linking.dedupe(capture.links)
    attach_quotes(capture.links, quotes or [])
    return capture


def attach_quotes(targets: list[Link], quotes: list[str]) -> None:
    """Pair --quote texts with links in order: Slack messages first, else any link."""
    quotes = [q.strip() for q in quotes if q.strip()]
    if not quotes:
        return
    open_slack = [link for link in targets if link.kind == LinkKind.SLACK and not link.quote]
    pool = open_slack or [link for link in targets if not link.quote]
    if len(quotes) > len(pool):
        what = "Slack link" if open_slack else "link"
        raise ToddError(
            f"There {'is' if len(quotes) == 1 else 'are'} {len(quotes)} "
            f"quote{'s' if len(quotes) != 1 else ''} but only {len(pool)} "
            f"{what}{'s' if len(pool) != 1 else ''} to attach "
            f"{'it' if len(quotes) == 1 else 'them'} to.",
            hint="Each [bold]-q[/] goes with the next Slack link, in the order you gave them.",
        )
    for link, quote in zip(pool, quotes, strict=False):
        link.quote = quote


def _link_line(line: str, site: str | None) -> Link | None:
    text = line.strip()
    text = text.removeprefix("- ").removeprefix("* ").strip()
    if not text or " " in text:
        return None
    return linking.recognize(text, site=site)


def _unquote(lines: list[str]) -> str | None:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if not lines:
        return None
    if all(line.lstrip().startswith(">") or not line.strip() for line in lines):
        lines = [line.lstrip().removeprefix(">").removeprefix(" ") for line in lines]
    return "\n".join(line.rstrip() for line in lines)


def parse_text(
    text: str,
    *,
    site: str | None = None,
    keys: Collection[str] = (),
    comments: bool = False,
) -> Capture:
    lines = text.splitlines()
    if comments:
        lines = [line for line in lines if not line.startswith("#")]
    head: list[str] = []
    sections: list[tuple[Link, list[str]]] = []
    for line in lines:
        if link := _link_line(line, site):
            sections.append((link, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            head.append(line)
    description = "\n".join(head).strip()
    found = linking.in_text(description, site=site, keys=keys)
    for link, body in sections:
        link.quote = _unquote(body)
        found.append(link)
    return Capture(description, linking.dedupe(found))


def to_text(capture: Capture) -> str:
    """The editor buffer for a capture, so you can add to what you typed."""
    parts = [capture.description.strip() or ""]
    for link in capture.links:
        block = link.url or link.ref or ""
        if link.quote:
            block += "\n" + link.quote
        elif link.kind == LinkKind.SLACK:
            block += "\n"
        parts.append(block)
    return "\n\n".join(parts).rstrip() + "\n\n" + EDITOR_HELP
