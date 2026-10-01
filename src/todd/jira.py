"""Jira, through Atlassian's own CLI (`acli`).

Commands used:
  acli jira workitem view KEY --json --fields ...
  acli jira workitem transition --key KEY --status "In Review" --yes
  acli jira auth status
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from todd import proc
from todd.errors import ToddError
from todd.links import jira_url

FIELDS = "key,issuetype,summary,status,assignee,priority,description"
DESCRIPTION_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class Ticket:
    key: str
    summary: str | None = None
    status: str | None = None
    type: str | None = None
    assignee: str | None = None
    priority: str | None = None
    description: str | None = None
    url: str | None = None


def adf_text(node: Any) -> str:
    """Flatten Atlassian Document Format (Jira's rich text JSON) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    match kind:
        case "text":
            return node.get("text", "")
        case "hardBreak":
            return "\n"
        case "mention" | "emoji" | "status":
            return attrs.get("text") or attrs.get("shortName") or ""
        case "inlineCard" | "blockCard" | "embedCard":
            return attrs.get("url", "")
        case "date":
            return str(attrs.get("timestamp", ""))
    inner = adf_text(node.get("content", []))
    match kind:
        case "listItem":
            return "- " + inner.strip() + "\n"
        case "bulletList" | "orderedList":
            return inner + "\n"
        case "paragraph" | "heading" | "blockquote" | "codeBlock" | "panel" | "rule":
            return inner.rstrip() + "\n\n"
        case "tableCell" | "tableHeader":
            return inner.strip() + " | "
        case "tableRow":
            return inner.rstrip(" |") + "\n"
    return inner


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("name") or value.get("value")
    return value if isinstance(value, str) else None


def parse_ticket(data: Any, *, key: str, site: str | None = None) -> Ticket:
    """Read `acli jira workitem view --json` output (Jira's REST shape: key + fields)."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ToddError(f"acli returned something unexpected for {key}.", detail=repr(data)[:200])
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else data
    description = adf_text(fields.get("description")).strip()
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT].rstrip() + "…"
    url = jira_url(data.get("key") or key, site)
    if url is None and isinstance(data.get("self"), str):
        host = urlsplit(data["self"]).hostname
        url = jira_url(data.get("key") or key, host)
    return Ticket(
        key=data.get("key") or key,
        summary=fields.get("summary"),
        status=_name(fields.get("status")),
        type=_name(fields.get("issuetype")),
        assignee=_name(fields.get("assignee")),
        priority=_name(fields.get("priority")),
        description=description or None,
        url=url,
    )


class Jira:
    def __init__(self, command: str = "acli", site: str | None = None, timeout: float = 30):
        self.command, self.site, self.timeout = command, site, timeout

    def _run(self, *args: str) -> proc.Result:
        try:
            return proc.run([self.command, "jira", *args], timeout=self.timeout)
        except proc.ProcError as e:
            hint = None
            if e.missing:
                hint = (
                    "Install the Atlassian CLI and sign in with [bold]acli jira auth login[/], "
                    "or set [bold]command[/] under [bold][jira][/] in your todd config."
                )
            raise ToddError(str(e), hint=hint) from e

    def view(self, key: str) -> Ticket:
        result = self._run("workitem", "view", key, "--json", "--fields", FIELDS)
        if not result.ok:
            raise ToddError(
                f"Couldn't read {key} from Jira.",
                detail=result.complaint,
                hint="Check you're signed in: [bold]acli jira auth status[/]",
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ToddError(
                f"acli didn't return JSON for {key}.", detail=result.stdout.strip()[:300]
            ) from e
        return parse_ticket(data, key=key, site=self.site)

    def transition(self, key: str, status: str) -> None:
        result = self._run("workitem", "transition", "--key", key, "--status", status, "--yes")
        if not result.ok:
            raise ToddError(
                f"Couldn't move {key} to {status}.",
                detail=result.complaint,
                hint=(
                    f"Check that [bold]{status}[/] is reachable from {key}'s current status "
                    "in its workflow, and matches your todd config."
                ),
            )

    def auth_status(self) -> proc.Result:
        return self._run("auth", "status")
