"""Regenerate the README screenshots in docs/.

    uv run scripts/screenshots.py

Nothing here talks to Claude, Jira or GitHub. todd's real commands run against a throwaway
database, with the stand-ins the tests use (tests/conftest.py) answering for `claude`, `acli`
and `gh` from the made-up fixtures in tests/fixtures. What they print is recorded and saved
with rich's SVG export.
"""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import FakeShell, load  # noqa: E402
from todd import ask, cli, db, proc, render, store  # noqa: E402
from todd.models import (  # noqa: E402
    Followup,
    Link,
    LinkKind,
    Priority,
    Role,
    State,
    Task,
)

DOCS = ROOT / "docs"
PROMPT = "\u276f "  # a shell-prompt chevron
TODAY = date(2026, 9, 30)  # a Wednesday
STACK = "https://github.com/example/billing/pull/86"

THEME = TerminalTheme(
    background=(16, 18, 25),
    foreground=(226, 228, 236),
    normal=[
        (32, 34, 44),
        (242, 80, 110),
        (31, 191, 143),
        (235, 154, 18),
        (124, 131, 247),
        (168, 113, 247),
        (86, 182, 194),
        (200, 202, 212),
    ],
    bright=[
        (92, 96, 112),
        (255, 110, 136),
        (70, 214, 170),
        (250, 184, 60),
        (152, 158, 255),
        (190, 146, 255),
        (120, 208, 220),
        (255, 255, 255),
    ],
)


class Quiet:
    """A spinner that doesn't spin: they come and go, and a picture can't show that."""

    def __enter__(self) -> Quiet:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def update(self, *_: object, **__: object) -> None:
        return None


class Terminal(Console):
    def status(self, *_: object, **__: object) -> Quiet:  # ty: ignore[invalid-method-override]
        return Quiet()


def terminal(width: int) -> Terminal:
    return Terminal(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        file=io.StringIO(),  # record only; don't echo
    )


class Stop(Exception):
    """Raised to end a picture at a question, before it's answered."""


class Chooser:
    """Stands in for the arrow-key chooser: draws the question with the highlight on the
    answer, then gives that answer."""

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}
        self.stop_at: str | None = None

    def __call__(self, question, options, *, default=0, console: Console, **_) -> str:
        keys = [option.key for option in options]
        picked = next((a for q, a in self.answers.items() if q in str(question)), keys[default])
        console.print(ask.line(question, options, keys.index(picked)))
        if self.stop_at and self.stop_at in str(question):
            raise Stop
        return picked


class World:
    """A throwaway todd: its own database and settings, and stand-ins for the tools it runs."""

    def __init__(self, home: Path) -> None:
        self.args = ["--db", str(home / "todd.sqlite"), "--config", str(home / "config.toml")]
        settings = '[jira]\nsite = "example.atlassian.net"\nkeys = ["PLAT"]\n'
        (home / "config.toml").write_text(settings)
        self.conn = db.connect(home / "todd.sqlite")
        self.shell = FakeShell()
        # The fixtures' made-up organisation, as the plainly made-up "example".
        self.shell.pulls = renamed(self.shell.pulls)
        self.shell.stacks = renamed(self.shell.stacks)
        self.chooser = Chooser()
        # todd's own seams, the ones the tests use: every program it runs, every question it
        # asks, and what day it is.
        setattr(proc, "run", self.shell)  # noqa: B010
        setattr(ask, "choose", self.chooser)  # noqa: B010
        setattr(cli, "_today", lambda: TODAY)  # noqa: B010
        setattr(cli, "_interactive", lambda: True)  # noqa: B010

    def ticket(self, key: str, summary: str, status: str) -> None:
        made = renamed(copy.deepcopy(load("acli_view_PLAT-412")))
        made["key"], made["fields"]["summary"] = key, summary
        made["fields"]["status"]["name"] = status
        self.shell.tickets[key] = made

    def run(self, console: Console, *argv: str, shown: str | None = None) -> None:
        """Run a todd command as if typed, recording everything it prints."""
        cli.out = cli.err = console
        if shown is not None:
            console.print(Text.assemble((PROMPT, f"bold {render.ACCENT}"), (shown, "bold")))
        with suppress(Stop):
            cli.command_group().main(
                args=[*self.args, *argv], prog_name="todd", standalone_mode=False
            )

    def quietly(self, *argv: str) -> None:
        self.run(terminal(120), *argv)

    def add(self, title: str, state: State = State.TODO, *, ago: timedelta, **fields) -> int:
        """Put a task straight into the database, as if filed a while back."""
        own = {
            k: fields.pop(k)
            for k in ("project_id", "project_position", "is_project")
            if k in fields
        }
        waiting_on = fields.pop("waiting_on", None)
        task = Task(title=title, links=fields.pop("links", []), **own)
        store.add(self.conn, task)
        assert task.id is not None
        with db.tx(self.conn):
            store.update(self.conn, task.id, triaged_at=store.now(), **fields)
            store.move(self.conn, task.id, state, waiting_on=waiting_on)
            self.conn.execute(
                "UPDATE task SET state_at = ? WHERE id = ?",
                (store.stamp(datetime.now().astimezone() - ago), task.id),
            )
        return task.id


def renamed(fixture):
    return json.loads(json.dumps(fixture).replace("acme", "example"))


def jira(key: str, title: str, status: str) -> Link:
    url = f"https://example.atlassian.net/browse/{key}"
    return Link(LinkKind.JIRA, url, ref=key, title=title, status=status, role=Role.TICKET)


def slack(quote: str, author: str) -> Link:
    url = "https://example.slack.com/archives/D024BE91L/p1790776800123456"
    return Link(
        LinkKind.SLACK,
        url,
        ref="D024BE91L/1790776800.123456",
        quote=quote,
        author=author,
        role=Role.RESPOND,
    )


def furnish(world: World) -> None:
    """What was already on the list before the pictures start."""
    hours, days = (lambda n: timedelta(hours=n)), (lambda n: timedelta(days=n, hours=3))
    world.ticket("PLAT-500", "Move webhooks to the new signing scheme", "In Progress")
    world.add(
        "Send Priya the Q3 migration numbers",
        ago=hours(20),
        priority=Priority.HIGH,
        due=TODAY + timedelta(days=1),
        area="platform",
        next_action="Pull the Q3 numbers from the migration dashboard",
        links=[
            slack("Can you send me the Q3 migration numbers before Thursday's sync?", "Priya"),
            jira("PLAT-77", "Q3 migration report", "To Do"),
        ],
    )
    webhooks = world.add("Webhook signing migration", ago=days(6), is_project=True, area="platform")
    webhook = world.add(
        "Ship the signing webhook",
        State.DOING,
        ago=hours(3),
        project_id=webhooks,
        project_position=1,
        due=TODAY + timedelta(days=2),
        next_action="Finish the retry handling",
        links=[jira("PLAT-500", "Move webhooks to the new signing scheme", "In Progress")],
    )
    runbook = world.add(
        "Write the webhook runbook",
        ago=days(6),
        project_id=webhooks,
        project_position=2,
        next_action="Outline the failure modes",
    )
    world.add(
        "Update the partner docs",
        ago=days(6),
        project_id=webhooks,
        project_position=3,
        next_action="List the endpoints that changed",
    )
    with db.tx(world.conn):
        store.add_blocker(world.conn, runbook, webhook)
    rfc = world.add(
        "Write the tenant isolation RFC",
        State.IN_REVIEW,
        ago=days(2),
        area="platform",
        next_action="Answer Mike's comments",
    )
    store.add_followup(
        world.conn, rfc, Followup(action="Tell Mike R the review slips to next week", due=TODAY)
    )
    world.add(
        "Renew the vendor contract",
        ago=days(9),
        due=date(2026, 11, 20),
        defer_until=date(2026, 11, 2),
        next_action="Ask legal for the draft",
    )
    following = world.add("Ledger write-split refactor", State.FOLLOWING, ago=days(4))
    store.add_followup(
        world.conn,
        following,
        Followup(action="Check in with Dana on the ledger refactor", due=date(2026, 10, 14)),
    )
    world.add("Fix the flaky deploy job", State.DONE, ago=hours(5))
    store.renumber(world.conn)
    for login, name in (
        ("adamtrain", "you"),
        ("sam-k", "Sam"),
        ("luke-p", "Luke"),
        ("example/platform-reviewers", "Platform reviewers"),
    ):
        world.quietly("nick", login, name)


def your_stack(world: World) -> None:
    """The fixture stack, as yours: waiting on other people's reviews, with its ticket named
    in a title."""
    pulls = world.shell.pulls
    for pull in pulls.values():
        pull["author"]["login"] = "adamtrain"
    pulls["86"]["title"] = "(PLAT-412) Move billing-worker to cluster-b"
    pulls["86"]["latestReviews"]["nodes"] = pulls["86"]["reviews"]["nodes"] = []
    pulls["86"]["reviewDecision"] = None
    pulls["88"]["reviewRequests"]["nodes"] = [
        {"requestedReviewer": {"__typename": "User", "login": "luke-p"}}
    ]
    world.ticket("PLAT-412", "Migrate billing workers to the new cluster", "In Review")
    world.ticket("PLAT-413", "Bug bash: billing on cluster-b", "To Do")
    world.ticket("PLAT-414", "Enable cluster-b billing for the first customer", "To Do")


def launch_filing() -> dict:
    """What Claude would answer for the capture in the picture."""

    def task(title: str, links: list[int], after: list[int], **more) -> dict:
        return {
            "title": title,
            "needs_title": False,
            "next_action": more.pop("next_action"),
            "track": "todo",
            "priority": "normal",
            "due": None,
            "due_hint": None,
            "defer": None,
            "people": [],
            "waiting_on": None,
            "follow_ups": [],
            "links": links,
            "after": after,
            **more,
        }

    return {
        "title": "Launch billing on cluster-b",
        "needs_title": False,
        "next_action": "Get the last two reviews on the cluster-b stack",
        "track": "todo",
        "area": "platform",
        "priority": "normal",
        "due": None,
        "due_hint": None,
        "defer": None,
        "people": ["Luke"],
        "waiting_on": None,
        "follow_ups": [],
        "links": [
            {"index": 1, "role": "deliverable", "note": "config split, merged", "author": None},
            {"index": 2, "role": "deliverable", "note": "the move itself", "author": None},
            {"index": 3, "role": "deliverable", "note": "production cutover", "author": None},
            {"index": 4, "role": "ticket", "note": "tracks the stack", "author": None},
            {"index": 5, "role": "ticket", "note": "the bug bash", "author": None},
            {"index": 6, "role": "ticket", "note": "turning it on", "author": None},
        ],
        "tasks": [
            task(
                "Get the cluster-b stack reviewed and merged",
                [2, 4],
                [],
                track="waiting",
                waiting_on="reviews from Luke and Platform reviewers",
                next_action="Nudge Luke and the platform reviewers",
            ),
            task("Run the bug bash", [5], [1], next_action="Book a room and write the test plan"),
            task(
                "Turn it on for the first customer",
                [6],
                [2],
                next_action="Flip the flag in the admin console",
                follow_ups=[
                    {
                        "action": "Tell Dana it's live",
                        "person": "Dana",
                        "when": "done",
                        "due": None,
                        "unless": None,
                    }
                ],
            ),
        ],
    }


def save(console: Console, name: str, title: str) -> None:
    if "--text" in sys.argv:  # to read what a picture says without opening it
        print(f"===== {name}\n{console.export_text(clear=False)}")
    path = DOCS / f"{name}.svg"
    console.save_svg(str(path), title=title, theme=THEME)
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as home:
        world = World(Path(home))
        furnish(world)
        your_stack(world)

        # Capturing a chain of work: one line, three tickets, a stack; stopped at the question.
        capture = terminal(100)
        world.shell.answers.append(launch_filing())
        world.chooser.stop_at = "File it?"
        said = (
            f'todd add "Waiting on reviews" {STACK} \\\n'
            '    "then a bug bash" PLAT-413 "then turn it on for the first customer" PLAT-414'
        )
        world.run(
            capture,
            "add",
            "Waiting on reviews",
            STACK,
            "then a bug bash",
            "PLAT-413",
            "then turn it on for the first customer",
            "PLAT-414",
            shown=said,
        )
        save(capture, "capture", "todd add")
        world.chooser.stop_at = None
        world.shell.answers.append(launch_filing())
        world.quietly("triage", "-y")

        # What you can act on now.
        hero = terminal(100)
        world.run(hero, shown="todd")
        save(hero, "hero", "todd")

        # Everything open, by project.
        everything = terminal(100)
        world.run(everything, "ls", shown="todd ls")
        save(everything, "ls", "todd ls")

        # Saying it in your own words.
        words = "the webhook shipped: start the runbook, and put the partner docs off until Monday"
        world.shell.answers.append(
            {
                "steps": [
                    {"argv": ["done", "3"], "says": "Finish #3 Ship the signing webhook"},
                    {"argv": ["start", "4"], "says": "Start #4 Write the webhook runbook"},
                    {
                        "argv": ["defer", "5", "mon"],
                        "says": "Put off #5 Update the partner docs until Monday",
                    },
                ],
                "question": None,
                "then": None,
            }
        )
        world.chooser.answers = {"Move PLAT-500 to Done?": "yes"}
        say = terminal(100)
        world.run(say, words, shown=f'todd "{words}"')
        save(say, "say", "todd")


if __name__ == "__main__":
    main()
