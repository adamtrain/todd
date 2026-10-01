"""Shared test setup.

Nothing here talks to Jira, Slack, GitHub or Claude: `FakeShell` stands in for every program
todd runs, answering from made-up fixtures in the shapes the real tools use.
"""

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from todd import ask, cli, proc, system
from todd.proc import ProcError, Result

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 30)  # a Wednesday

SLACK_DM = "https://acme.slack.com/archives/D024BE91L/p1790776800123456"
SLACK_THREAD = (
    "https://acme.slack.com/archives/C01ABCDEF/p1790780400000200"
    "?thread_ts=1790674200.000100&cid=C01ABCDEF"
)
PR_URL = "https://github.com/acme/billing/pull/86"  # the middle of a stack: 85 → 86 → 88
SOLO_PR_URL = "https://github.com/acme/billing/pull/90"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def answer(**changes: Any) -> dict[str, Any]:
    """A made-up Claude filing: the fixture's, with some fields changed."""
    return {**load("claude_envelope")["structured_output"], **changes}


@dataclass
class Call:
    argv: list[str]
    input: str | None


@dataclass
class FakeShell:
    tickets: dict[str, Any] = field(default_factory=dict)
    answers: list[Any] = field(default_factory=list)  # dict: structured, str: text, Result: raw
    calls: list[Call] = field(default_factory=list)
    transitions: list[tuple[str, str]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)
    transition_error: str | None = None
    stacks: list[Any] = field(default_factory=lambda: load("gh_stacks"))
    pulls: dict[str, Any] = field(default_factory=lambda: load("gh_pulls"))
    stacks_error: str | None = None

    def __call__(self, argv, *, input=None, timeout=None, cwd=None) -> Result:
        self.calls.append(Call(list(argv), input))
        command = argv[0]
        if command in self.missing:
            raise ProcError(f"{command} isn't installed or isn't on your PATH.", missing=True)
        if argv[1:] == ["--version"]:
            return Result(0, f"{command} 9.9.9\n", "")
        match command:
            case "acli":
                return self._acli(argv[1:])
            case "claude":
                return self._claude()
            case "gh":
                return self._gh(argv[1:])
            case "open" | "xdg-open":
                self.opened.append(argv[1])
                return Result(0, "", "")
        raise AssertionError(f"todd ran something unexpected: {argv}")

    def _acli(self, args: list[str]) -> Result:
        if args[:2] == ["jira", "auth"]:
            return Result(0, "✓ Authenticated\n  Site: acme.atlassian.net\n", "")
        if args[:3] == ["jira", "workitem", "view"]:
            key = args[3]
            if key not in self.tickets:
                return Result(1, "", f"✗ Error: {key} doesn't exist or you can't see it")
            return Result(0, json.dumps(self.tickets[key]), "")
        if args[:3] == ["jira", "workitem", "transition"]:
            key = args[args.index("--key") + 1]
            status = args[args.index("--status") + 1]
            assert "--yes" in args, "todd must never leave acli waiting on a prompt"
            if self.transition_error:
                return Result(1, "", self.transition_error)
            self.transitions.append((key, status))
            if key in self.tickets:
                self.tickets[key]["fields"]["status"]["name"] = status
            return Result(0, f"✓ Work item {key} has been transitioned", "")
        raise AssertionError(f"unexpected acli call: {args}")

    def _gh(self, args: list[str]) -> Result:
        if args[:2] == ["api", "graphql"]:
            query = next(a for a in args if a.startswith("query="))
            numbers = re.findall(r"pr(\d+): pullRequest", query)
            found = {f"pr{n}": self.pulls[n] for n in numbers if n in self.pulls}
            data: dict[str, Any] = {"data": {"viewer": {"login": "adamtrain"}, "repository": found}}
            gone = [n for n in numbers if n not in self.pulls]
            if not gone:
                return Result(0, json.dumps(data), "")
            message = f"Could not resolve to a PullRequest with the number of {gone[0]}."
            data["errors"] = [{"type": "NOT_FOUND", "message": message}]
            return Result(1, json.dumps(data), f"gh: {message}")
        if args[0] == "api" and "/stacks?pull_request=" in args[-1]:
            assert "X-GitHub-Api-Version: 2026-03-10" in args
            if self.stacks_error:
                return Result(1, "", self.stacks_error)
            number = int(args[-1].rsplit("=", 1)[1])
            matching = [
                stack
                for stack in self.stacks
                if number in [pr["number"] for pr in stack["pull_requests"]]
            ]
            return Result(0, json.dumps(matching), "")
        if args[:2] == ["issue", "view"]:
            issue = {
                "title": "Billing retries double-charge",
                "state": "OPEN",
                "author": {"login": "sam-k"},
                "body": "Seen twice this week.",
            }
            return Result(0, json.dumps(issue), "")
        raise AssertionError(f"unexpected gh call: {args}")

    def _claude(self) -> Result:
        answer = (
            self.answers.pop(0) if self.answers else load("claude_envelope")["structured_output"]
        )
        if isinstance(answer, Result):
            return answer
        envelope = load("claude_envelope")
        if isinstance(answer, dict):
            envelope["structured_output"] = answer
            envelope["result"] = json.dumps(answer)
        else:
            envelope.pop("structured_output")
            envelope["result"] = answer
        return Result(0, json.dumps(envelope), "")

    @property
    def prompts(self) -> list[str]:
        return [c.input or "" for c in self.calls if c.argv[0] == "claude" and c.input]

    def ran(self, *words: str) -> list[Call]:
        return [c for c in self.calls if all(w in c.argv for w in words)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config and the real date."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TODD_DB", str(tmp_path / "todd.sqlite"))
    monkeypatch.setenv("TODD_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    return tmp_path


@pytest.fixture
def shell(monkeypatch) -> FakeShell:
    fake = FakeShell(tickets={"PLAT-412": copy.deepcopy(load("acli_view_PLAT-412"))})
    monkeypatch.setattr(proc, "run", fake)
    return fake


@dataclass
class Picks:
    """Stands in for the arrow-key chooser: answers from a script, else takes the default."""

    script: list[str] = field(default_factory=list)
    asked: list[tuple[str, list[str], str]] = field(default_factory=list)  # question, keys, default
    on_ask: Any = None  # called each time something is asked, to look at things meanwhile

    def __call__(self, question, options, *, default=0, console=None, **_):
        if self.on_ask is not None:
            self.on_ask()
        keys = [o.key for o in options]
        self.asked.append((str(question), keys, keys[default]))
        picked = self.script.pop(0) if self.script else keys[default]
        assert picked in keys, f"{picked!r} isn't one of {keys} for {question!r}"
        return picked


@pytest.fixture
def picks(monkeypatch) -> Picks:
    """A terminal to choose in, with the answers you script (or the defaults)."""
    chooser = Picks()
    monkeypatch.setattr(ask, "choose", chooser)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    return chooser


@pytest.fixture
def clipboard(monkeypatch) -> list[str]:
    copied: list[str] = []
    monkeypatch.setattr(system, "copy", copied.append)
    return copied


@pytest.fixture
def config_file(isolated):
    path = isolated / "config.toml"

    def write(text: str) -> Path:
        path.write_text(text)
        return path

    return write
