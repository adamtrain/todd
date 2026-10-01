"""Settings from ~/.config/todd/config.toml. Everything has a sensible default."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from todd.errors import ToddError
from todd.models import State
from todd.models import parse_state as models_parse_state

EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_JIRA_STATUS = {
    State.DOING: "In Progress",
    State.IN_REVIEW: "In Review",
    State.DONE: "Done",
}

TEMPLATE = """\
# todd configuration. Every setting is optional; these are the defaults unless noted.

[claude]
# command = "claude"
# model = "opus"        # default: whatever Claude Code uses
# effort = "low"        # low, medium, high, xhigh or max
# timeout = 120         # seconds
# args = []             # extra arguments for `claude -p`, e.g. ["--safe-mode"]

[jira]
# command = "acli"
# site = "yourcompany.atlassian.net"   # lets todd link bare keys like PROJ-123
# keys = ["PROJ", "OPS"]              # project keys to spot inside your task text

# The Jira status to move a ticket to when its task enters each todd state
# (todo, doing, waiting, in_review, done, dropped, following). Unlisted states leave Jira
# alone. todd asks before every change; -y makes it without asking, --no-jira skips it.
[jira.status]
doing = "In Progress"
in_review = "In Review"
done = "Done"

# Projects with a different workflow can override any of those. "" means leave Jira alone.
# [jira.projects.OPS.status]
# in_review = ""
# done = "Closed"

[slack]
# ask_for_text = true                 # offer to paste a Slack message's text when you add its link
# prompt_on = ["in_review", "done"]   # states that ask whether to reply in Slack

[github]
# fetch = true                        # read pull requests (and their stacks) and issues with `gh`

[following]
# check_in_days = 14                  # when to check back on something you're following,
                                      # if you didn't say

# Projects Claude should file tasks under, with a hint about what belongs in each.
[projects]
# platform = "Infrastructure, CI, migrations"
# hiring = "Interviews, debriefs, hiring loops"
"""


@dataclass(slots=True)
class ClaudeConfig:
    command: str = "claude"
    model: str | None = None
    effort: str | None = "low"
    timeout: float = 120.0
    args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class JiraConfig:
    command: str = "acli"
    site: str | None = None
    keys: list[str] = field(default_factory=list)
    status: dict[State, str] = field(default_factory=lambda: dict(DEFAULT_JIRA_STATUS))
    projects: dict[str, dict[State, str]] = field(default_factory=dict)

    def target(self, key: str, state: State) -> str | None:
        """The Jira status a ticket should move to when its task enters `state`."""
        project = key.rsplit("-", 1)[0].upper()
        overrides = self.projects.get(project, {})
        status = overrides[state] if state in overrides else self.status.get(state)
        return status or None

    @property
    def known_keys(self) -> set[str]:
        return {k.upper() for k in self.keys} | set(self.projects)


@dataclass(slots=True)
class SlackConfig:
    ask_for_text: bool = True
    prompt_on: frozenset[State] = frozenset({State.IN_REVIEW, State.DONE})


@dataclass(slots=True)
class FollowingConfig:
    check_in_days: int = 14


@dataclass(slots=True)
class GitHubConfig:
    fetch: bool = True


@dataclass(slots=True)
class Config:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    following: FollowingConfig = field(default_factory=FollowingConfig)
    projects: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    loaded: bool = False


# ── Parsing ──────────────────────────────────────────────────────────────────


class _Reader:
    """Pulls typed values out of a TOML table, complaining clearly about anything odd."""

    def __init__(self, table: dict[str, Any], where: str, allowed: set[str]):
        unknown = sorted(set(table) - allowed)
        if unknown:
            raise ToddError(
                f"Unknown setting{'s' if len(unknown) > 1 else ''} in [{where}]: "
                + ", ".join(unknown),
                hint="Allowed: " + ", ".join(sorted(allowed)),
            )
        self.table, self.where = table, where

    def get(self, name: str, kind: type | tuple[type, ...], default: Any) -> Any:
        if name not in self.table:
            return default
        value = self.table[name]
        # bool is an int in Python; don't let `timeout = true` through.
        if not isinstance(value, kind) or (isinstance(value, bool) and bool not in _as_tuple(kind)):
            raise ToddError(f"[{self.where}] {name} has the wrong type: {value!r}")
        return value

    def text(self, name: str, default: str | None) -> str | None:
        value = self.get(name, str, default)
        return value.strip() or None if isinstance(value, str) else value

    def strings(self, name: str) -> list[str]:
        value = self.get(name, list, [])
        if not all(isinstance(v, str) for v in value):
            raise ToddError(f"[{self.where}] {name} should be a list of strings.")
        return value


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


def parse_state(text: str, where: str) -> State:
    state = models_parse_state(text)
    if state is None:
        raise ToddError(
            f"[{where}] mentions {text!r}, which isn't a todd state.",
            hint="States: " + ", ".join(s.value for s in State if s != State.INBOX),
        )
    return state


def _statuses(table: Any, where: str) -> dict[State, str]:
    if not isinstance(table, dict):
        raise ToddError(f'[{where}] should be a table of state = "Jira status".')
    mapping: dict[State, str] = {}
    for name, status in table.items():
        if not isinstance(status, str):
            raise ToddError(f"[{where}] {name} should be a Jira status name in quotes.")
        mapping[parse_state(name, where)] = status.strip()
    return mapping


def parse(data: dict[str, Any]) -> Config:
    top = _Reader(data, "top level", {"claude", "jira", "slack", "github", "following", "projects"})
    config = Config()

    c = _Reader(
        top.get("claude", dict, {}), "claude", {"command", "model", "effort", "timeout", "args"}
    )
    effort = c.text("effort", "low")
    if effort is not None and effort not in EFFORTS:
        raise ToddError(f"[claude] effort must be one of {', '.join(EFFORTS)}, not {effort!r}.")
    config.claude = ClaudeConfig(
        command=c.text("command", "claude") or "claude",
        model=c.text("model", None),
        effort=effort,
        timeout=float(c.get("timeout", (int, float), 120)),
        args=c.strings("args"),
    )

    j = _Reader(
        top.get("jira", dict, {}),
        "jira",
        {"command", "site", "keys", "status", "projects"},
    )
    projects: dict[str, dict[State, str]] = {}
    for key, table in j.get("projects", dict, {}).items():
        where = f"jira.projects.{key}"
        if not isinstance(table, dict):
            raise ToddError(f"[{where}] should be a table.")
        _Reader(table, where, {"status"})
        projects[key.upper()] = _statuses(table.get("status", {}), f"{where}.status")
    config.jira = JiraConfig(
        command=j.text("command", "acli") or "acli",
        site=j.text("site", None),
        keys=j.strings("keys"),
        status=(
            _statuses(j.table["status"], "jira.status")
            if "status" in j.table
            else dict(DEFAULT_JIRA_STATUS)
        ),
        projects=projects,
    )

    s = _Reader(top.get("slack", dict, {}), "slack", {"ask_for_text", "prompt_on"})
    prompt_on = s.table.get("prompt_on")
    config.slack = SlackConfig(
        ask_for_text=s.get("ask_for_text", bool, True),
        prompt_on=(
            frozenset(parse_state(v, "slack") for v in s.strings("prompt_on"))
            if prompt_on is not None
            else SlackConfig().prompt_on
        ),
    )

    g = _Reader(top.get("github", dict, {}), "github", {"fetch"})
    config.github = GitHubConfig(fetch=g.get("fetch", bool, True))

    f = _Reader(top.get("following", dict, {}), "following", {"check_in_days"})
    days = f.get("check_in_days", int, 14)
    if days < 1:
        raise ToddError("[following] check_in_days should be at least 1.")
    config.following = FollowingConfig(check_in_days=days)

    raw_projects = top.get("projects", dict, {})
    for name, hint in raw_projects.items():
        if not isinstance(hint, str):
            raise ToddError(f"[projects] {name} should be a short description in quotes.")
    config.projects = {name: hint.strip() for name, hint in raw_projects.items()}
    return config


def load(path: Path) -> Config:
    if not path.exists():
        config = Config()
        config.path = path
        return config
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ToddError(f"{path} isn't valid TOML.", detail=str(e)) from e
    except OSError as e:
        raise ToddError(f"Couldn't read {path}.", detail=e.strerror) from e
    try:
        config = parse(data)
    except ToddError as e:
        e.detail = e.detail or f"in {path}"
        raise
    config.path, config.loaded = path, True
    return config
