"""`todd`: capture work tasks, let Claude file them, and keep Jira in step as you work them."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, TextIO

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from todd import __version__, ask, db, effects, render, store, system, triage
from todd import capture as capturing
from todd import config as configuration
from todd import links as linking
from todd.claude import Claude
from todd.config import Config
from todd.errors import ToddError
from todd.jira import Jira
from todd.models import (
    EntryKind,
    Followup,
    FollowupStatus,
    Kind,
    Link,
    LinkKind,
    Priority,
    Role,
    State,
    Task,
    parse_state,
)
from todd.people import Nicknames

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ── Plumbing ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Session:
    db_path: Path
    config_path: Path
    _config: Config | None = None
    _conn: sqlite3.Connection | None = None
    _names: Nicknames | None = None

    @property
    def names(self) -> Nicknames:
        """Nicknames for GitHub logins, from nicknames.toml beside the config."""
        if self._names is None:
            self._names = Nicknames(self.config_path.parent / "nicknames.toml")
        return self._names

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = configuration.load(self.config_path)
        return self._config

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db.connect(self.db_path)
        return self._conn


def _session(ctx: typer.Context) -> Session:
    session = ctx.find_root().obj
    assert isinstance(session, Session)
    return session


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _stdin_is_piped(stdin: TextIO) -> bool:
    """True when stdin is a pipe or redirected file, not a terminal or /dev/null."""
    try:
        mode = os.fstat(stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode)


def _today() -> date:
    return date.today()


def _fail(e: ToddError) -> typer.Exit:
    render.error(err, str(e), detail=e.detail, hint=e.hint)
    return typer.Exit(1)


def _yes(question: str | Text, *, default: bool) -> bool:
    """A yes-or-no question, answered with the arrow keys. No is always on the left."""
    picked = ask.choose(question, ask.YES_NO, default=1 if default else 0, console=err)
    return picked == "yes"


def _version(value: bool) -> None:
    if value:
        out.print(f"todd {__version__}")
        raise typer.Exit()


TaskId = Annotated[int, typer.Argument(help="The task's number.", show_default=False, min=1)]
Words = Annotated[
    list[str] | None,
    typer.Argument(help="A note to add to the task's timeline.", show_default=False),
]
Yes = Annotated[
    bool, typer.Option("--yes", "-y", help="Make the Jira changes without asking about each one.")
]
NoJira = Annotated[bool, typer.Option("--no-jira", "-J", help="Leave Jira alone, without asking.")]
Local = Annotated[
    bool, typer.Option("--local", "-l", help="Only change todd: leave Jira and Slack alone.")
]


def _plain(text: str) -> None:
    """Print a link on a line of its own with nothing around it, so terminals can spot it."""
    print(text, file=sys.stderr, flush=True)


EPILOG = (
    "[bold]Examples[/]\n\n"
    '  [cyan]todd add "reply to Priya re: Q3 numbers" https://acme.slack.com/archives/D…/p…[/]\n'
    "  [cyan]todd[/]                     your queue, with follow-ups that are due\n"
    "  [cyan]todd start 12[/]            begin #12 (asks about moving its Jira ticket)\n"
    "  [cyan]todd wait 12 Priya to confirm[/]\n"
    "  [cyan]todd done 12[/]             finish it: Jira, follow-ups, then reply in Slack\n"
    "  [cyan]todd links 12[/]            every link on #12, one per line\n"
    "  [cyan]todd following[/]           things you're keeping an eye on\n"
)


@app.callback(invoke_without_command=True, epilog=EPILOG)
def main_callback(
    ctx: typer.Context,
    db_file: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help=f"Database to use (default: ${db.DB_ENV} or ~/.config/todd).",
            show_default=False,
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=f"Config file (default: ${db.CONFIG_ENV} or ~/.config/todd).",
            show_default=False,
        ),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """A work to-do list that files tasks with [bold]Claude[/] and keeps [bold]Jira[/] in step.

    With no command, shows your queue.
    """
    ctx.obj = Session(db.db_path(db_file), db.config_path(config_file))
    if ctx.invoked_subcommand is None:
        _queue(ctx.obj)


def _queue(
    session: Session,
    *,
    closed: bool = False,
    project: str | None = None,
    kind: Kind | None = None,
    person: str | None = None,
) -> None:
    try:
        conn = session.conn
        states = list(render.QUEUE_ORDER) + (list(render.CLOSED_ORDER) if closed else [])
        who = session.names.aliases(person) if person else None
        tasks = store.tasks(conn, states, project=project, kind=kind, person=who)
        now = store.now()
        local = datetime.now().astimezone()
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0)
        done = store.closed_since(conn, monday)
        filtered = bool(project or kind or person)
        due = [] if filtered else _with_tasks(conn, store.open_followups(conn), due_only=True)
        following = store.counts(conn).get(State.FOLLOWING, 0)
    except ToddError as e:
        raise _fail(e) from e
    render.queue(
        out,
        tasks,
        today=_today(),
        now=now,
        done_this_week=done,
        closed=closed,
        due=due,
        following=0 if filtered else following,
        names=session.names,
    )


def _with_tasks(
    conn: sqlite3.Connection, followups: list[Followup], *, due_only: bool = False
) -> list[tuple[Followup, Task]]:
    today = _today()
    chosen = [f for f in followups if f.is_due(today)] if due_only else followups
    tasks: dict[int, Task] = {}
    for f in chosen:
        assert f.task_id is not None
        if f.task_id not in tasks:
            tasks[f.task_id] = store.get(conn, f.task_id)
    return [(f, tasks[f.task_id]) for f in chosen if f.task_id is not None]


# ── Looking at things ────────────────────────────────────────────────────────


@app.command("ls")
def list_tasks(
    ctx: typer.Context,
    everything: Annotated[
        bool, typer.Option("--all", "-a", help="Include done and dropped tasks.")
    ] = False,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Only this project.", show_default=False)
    ] = None,
    kind: Annotated[
        Kind | None,
        typer.Option("--kind", "-k", help="Only this kind of task.", show_default=False),
    ] = None,
    person: Annotated[
        str | None,
        typer.Option(
            "--person",
            help="Only tasks involving this person (a name, or a GitHub login).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Show your queue: what you're doing, what's next, and what's stuck."""
    _queue(_session(ctx), closed=everything, project=project, kind=kind, person=person)


@app.command()
def show(ctx: typer.Context, task_id: TaskId) -> None:
    """Everything about one task: its links, the messages you saved, and its timeline."""
    session = _session(ctx)
    try:
        task = store.get(session.conn, task_id)
    except ToddError as e:
        raise _fail(e) from e
    render.show(out, task, today=_today(), names=session.names)


# ── Capturing ────────────────────────────────────────────────────────────────


def _read_pasted(stdin: TextIO) -> str | None:
    """Read a pasted block: Enter alone skips; Ctrl-D (or two empty lines) finishes."""
    first = stdin.readline()
    if not first.strip():
        return None
    lines, blanks = [first], 0
    while line := stdin.readline():
        blanks = blanks + 1 if not line.strip() else 0
        if blanks >= 2:
            break
        lines.append(line)
    return "".join(lines).strip() or None


def _ask_for_messages(links: list[Link]) -> None:
    for link in links:
        if link.kind != LinkKind.SLACK or link.quote:
            continue
        err.print(
            Text.assemble(
                ("◇ ", render.SLACK),
                (linking.label(link), f"bold {render.SLACK}"),
                "  Paste the message so todd keeps it with this link.",
            )
        )
        err.print(
            Text(
                "  Enter to skip · after pasting, Ctrl-D (or two empty lines) to finish",
                style=render.FAINT,
            )
        )
        link.quote = _read_pasted(sys.stdin)


def _mark_reply_links(capture: capturing.Capture, targets: list[str], site: str | None) -> None:
    """Links given with --reply-in are where you'll reply, whatever Claude thinks."""
    for target in targets:
        link = linking.recognize(target, site=site)
        if link is None:
            raise ToddError(f"{target!r} isn't a link.")
        same = next((have for have in capture.links if have.target == link.target), None)
        if same is None:
            capture.links.append(link)
            same = link
        same.role, same.role_fixed = Role.RESPOND, True


def _provisional_title(capture: capturing.Capture) -> str:
    """Your own words until Claude files it, with links shown by name rather than URL."""
    first = capture.description.strip().splitlines()[0] if capture.description.strip() else ""
    words = []
    for word in first.split():
        link = linking.recognize(word)
        words.append(linking.label(link) if link else word)
    title = " ".join(words)
    if not title and capture.links:
        title = linking.label(capture.links[0])
    return title[:120] or "Untitled"


def _file(session: Session, task: Task, *, label: str = "Filing") -> Task:
    """Look up the task's links, then have Claude file it. Returns the task as saved."""
    assert task.id is not None
    config, conn = session.config, session.conn
    w = render.width(err)
    with err.status(render.stage("", label), spinner_style=render.ACCENT) as status:
        found = triage.gather(
            task.links,
            config,
            on_step=lambda step: status.update(render.stage(step, label)),
            names=session.names,
        )
    _print_lookups(session, found, w)
    triage.save_lookups(conn, task.id, found)
    with err.status(render.stage("asking Claude", label), spinner_style=render.ACCENT):
        filing = triage.ask(
            task,
            found,
            config,
            used_projects=store.projects(conn),
            today=_today(),
            names=session.names,
        )
    triage.apply(conn, task, filing, [g.link for g in found])
    filed = store.get(conn, task.id)
    if filed.needs_title and _interactive():
        _ask_for_title(session, filed)
        filed = store.get(conn, task.id)
    return filed


def _ask_for_title(session: Session, task: Task) -> None:
    """Claude couldn't tell what the task is, so ask rather than guess."""
    assert task.id is not None
    err.print(
        Text.assemble(
            ("? ", f"bold {render.WARN}"),
            "Claude couldn't tell what this is from what it could read.",
        )
    )
    title = Prompt.ask(
        Text.from_markup("  What should it be called? [dim](Enter to decide later)[/]"),
        console=err,
        default="",
        show_default=False,
    ).strip()
    if title:
        _set_title(session.conn, task.id, title)


def _set_title(conn: sqlite3.Connection, task_id: int, title: str) -> None:
    with db.tx(conn):
        store.update(conn, task_id, title=title, needs_title=False)
        store.log(conn, task_id, EntryKind.NOTE, "Titled")


def _print_lookups(session: Session, found: list[triage.Gathered], w: int) -> None:
    for g in found:
        problem = str(g.error) if g.error else None
        err.print(render.lookup_line(g.link, problem, w, names=session.names, new=g.new))


@app.command()
def add(
    ctx: typer.Context,
    words: Annotated[
        list[str] | None,
        typer.Argument(
            help="What needs doing, plus any links or Jira keys (in any order).",
            show_default=False,
        ),
    ] = None,
    quotes: Annotated[
        list[str] | None,
        typer.Option(
            "--quote",
            "-q",
            help="The text of a Slack message you linked, kept with that link. "
            "Repeat for several links, in order.",
            show_default=False,
        ),
    ] = None,
    edit: Annotated[
        bool, typer.Option("--edit", "-e", help="Write it (or finish it) in your editor.")
    ] = False,
    raw: Annotated[
        bool, typer.Option("--raw", help="Just save it to your inbox; don't look anything up.")
    ] = False,
    reply_in: Annotated[
        list[str] | None,
        typer.Option(
            "--reply-in",
            "-r",
            help="A Slack link where you'll need to reply (other Slack links are just context).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Capture a task. Claude files it: title, next step, due date, follow-ups and more.

    Mention follow-ups in your own words ("tell Theo when I'm done") and they're kept too.

    Pass links as arguments. For a Slack link, todd asks you to paste the message
    (or use [bold]-q[/]) so its text stays with the link even though todd can't read Slack.
    With no arguments, todd opens your editor, or reads piped text.
    """
    session = _session(ctx)
    try:
        config = session.config
        site, keys = config.jira.site, config.jira.known_keys
        interactive = _interactive()
        origin = "captured"
        if words:
            capture = capturing.from_args(words, quotes, site=site, keys=keys)
        elif _stdin_is_piped(sys.stdin):
            capture = capturing.parse_text(sys.stdin.read(), site=site, keys=keys)
            capturing.attach_quotes(capture.links, quotes or [])
            origin = "from stdin"
        elif interactive:
            capture, edit = capturing.Capture(), True
        else:
            raise ToddError(
                "Nothing to add.", hint='Describe the task: [bold]todd add "…" [links][/]'
            )
        if edit:
            text = system.edit(capturing.to_text(capture))
            capture = capturing.parse_text(text, site=site, keys=keys, comments=True)
            origin = "from your editor"
        _mark_reply_links(capture, reply_in or [], site)
        if capture.empty:
            raise ToddError("Nothing captured, so nothing was added.")
        if interactive and config.slack.ask_for_text and not edit:
            _ask_for_messages(capture.links)

        conn = session.conn
        for link in capture.links:
            if others := store.open_tasks_with(conn, link):
                render.warn(
                    err,
                    f"{linking.label(link)} is already on "
                    + ", ".join(f"#{n}" for n in others)
                    + ".",
                )
        task = Task(
            title=_provisional_title(capture),
            description=capture.description,
            links=capture.links,
        )
        store.add(conn, task)
        assert task.id is not None
        err.print(render.capture_line(capture, origin, render.width(err)))
        if raw:
            render.success(
                out,
                Text.assemble(
                    f"Saved #{task.id} to your inbox. ",
                    ("File it later with ", render.FAINT),
                    (f"todd triage {task.id}", "bold"),
                ),
            )
            return
    except ToddError as e:
        raise _fail(e) from e

    try:
        task = _file(session, task)
    except ToddError as e:
        render.error(err, str(e), detail=e.detail, hint=e.hint)
        render.warn(
            err,
            f"#{task.id} is saved in your inbox, unfiled.",
            hint=f"Try again with [bold]todd triage {task.id}[/].",
        )
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        err.print(Text(f"Stopped. #{task.id} is saved in your inbox, unfiled.", style=render.FAINT))
        raise typer.Exit(130) from None
    render.success(err, Text.assemble(f"Filed #{task.id} in ", render.state_word(task.state)))
    out.print(render.card(task, _today(), render.width(out), session.names))


@app.command("triage")
def triage_command(
    ctx: typer.Context,
    task_id: Annotated[
        int | None,
        typer.Argument(
            help="The task to (re)file. Default: everything in your inbox.", show_default=False
        ),
    ] = None,
) -> None:
    """Have Claude file a task again, or everything still in your inbox."""
    session = _session(ctx)
    try:
        conn = session.conn
        targets = [store.get(conn, task_id)] if task_id else store.tasks(conn, [State.INBOX])
        if not targets:
            render.success(err, "Your inbox is empty.")
            return
        for task in targets:
            err.print(Text.assemble(("◇ ", render.ACCENT), (f"#{task.id} ", "bold"), task.title))
            filed = _file(session, task, label="Refiling" if task.triaged else "Filing")
            render.success(
                err, Text.assemble(f"Filed #{filed.id} in ", render.state_word(filed.state))
            )
            out.print(render.card(filed, _today(), render.width(out), session.names))
    except ToddError as e:
        raise _fail(e) from e


@app.command("link")
def link_command(
    ctx: typer.Context,
    task_id: TaskId,
    targets: Annotated[
        list[str], typer.Argument(help="Links or Jira keys to add.", show_default=False)
    ],
    quotes: Annotated[
        list[str] | None,
        typer.Option(
            "--quote", "-q", help="A Slack message's text, kept with its link.", show_default=False
        ),
    ] = None,
) -> None:
    """Attach more links to a task (and, for Slack, the message they point at)."""
    session = _session(ctx)
    try:
        config, conn = session.config, session.conn
        task = store.get(conn, task_id)
        new: list[Link] = []
        for target in targets:
            link = linking.recognize(target, site=config.jira.site)
            if link is None:
                raise ToddError(f"{target!r} isn't a link or a Jira key.")
            new.append(link)
        have = {link.ref or link.url for link in task.links}
        for link in [link for link in new if (link.ref or link.url) in have]:
            render.warn(err, f"{linking.label(link)} is already on #{task_id}.")
        new = [link for link in linking.dedupe(new) if (link.ref or link.url) not in have]
        if not new:
            return
        capturing.attach_quotes(new, quotes or [])
        if _interactive() and config.slack.ask_for_text:
            _ask_for_messages(new)
        store.add_links(conn, task_id, new)
        found = triage.gather(new, config, names=session.names, known=task.links)
        triage.save_lookups(conn, task_id, found)
        _print_lookups(session, found, render.width(err))
        added = len(new) + sum(g.new for g in found)
        render.success(
            err,
            Text.assemble(
                f"Added {render.plural(added, 'link')} to #{task.id}. ",
                ("Refile with ", render.FAINT),
                (f"todd triage {task.id}", "bold"),
                (" to let Claude take them into account.", render.FAINT),
            ),
        )
    except ToddError as e:
        raise _fail(e) from e


@app.command()
def note(
    ctx: typer.Context,
    task_id: TaskId,
    words: Annotated[list[str], typer.Argument(help="The note.", show_default=False)],
) -> None:
    """Add a note to a task's timeline."""
    try:
        conn = _session(ctx).conn
        task = store.get(conn, task_id)
        store.note(conn, task_id, " ".join(words))
    except ToddError as e:
        raise _fail(e) from e
    render.success(err, Text.assemble(f"Noted on #{task.id}  ", (task.title, render.FAINT)))


def parse_due(text: str, today: date) -> date | None:
    t = text.strip().lower()
    if t in ("", "none", "no", "-"):
        return None
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t.startswith("+") and t[1:].rstrip("d").isdigit():
        return today + timedelta(days=int(t[1:].rstrip("d")))
    if t == "next week":
        return today + timedelta(days=7 - today.weekday())
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(names):
        if t in (name, name[:3]):
            return today + timedelta(days=(i - today.weekday()) % 7)
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise ToddError(
            f"{text!r} isn't a date todd understands.",
            hint="Use YYYY-MM-DD, today, tomorrow, a weekday like fri, +7, next week or none.",
        ) from None


@app.command()
def edit(
    ctx: typer.Context,
    task_id: TaskId,
    title: Annotated[str | None, typer.Option("--title", "-t", show_default=False)] = None,
    next_action: Annotated[
        str | None, typer.Option("--next", "-n", help="The next concrete step.", show_default=False)
    ] = None,
    kind: Annotated[Kind | None, typer.Option("--kind", "-k", show_default=False)] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help='A project, or "none".', show_default=False),
    ] = None,
    priority: Annotated[
        Priority | None, typer.Option("--priority", "-P", show_default=False)
    ] = None,
    due: Annotated[
        str | None,
        typer.Option(
            "--due", "-d", help="YYYY-MM-DD, today, tomorrow, fri… or none.", show_default=False
        ),
    ] = None,
    people: Annotated[
        str | None, typer.Option("--people", help="Comma-separated names.", show_default=False)
    ] = None,
) -> None:
    """Correct what Claude filed."""
    session = _session(ctx)
    try:
        conn = session.conn
        store.get(conn, task_id)
        fields: dict[str, object] = {}
        if title is not None:
            if not title.strip():
                raise ToddError("A title can't be empty.")
            fields["title"] = title.strip()
            fields["needs_title"] = False
        if next_action is not None:
            fields["next_action"] = next_action.strip() or None
        if kind is not None:
            fields["kind"] = kind
        if project is not None:
            fields["project"] = (
                None if project.strip().lower() in ("", "none") else project.strip().lower()
            )
        if priority is not None:
            fields["priority"] = priority
        if due is not None:
            fields["due"] = parse_due(due, _today())
        if not fields and people is None:
            raise ToddError(
                "Nothing to change.", hint="See [bold]todd edit --help[/] for what you can set."
            )
        with db.tx(conn):
            store.update(conn, task_id, **fields)
            if people is not None:
                store.set_people(conn, task_id, people.split(","))
            store.log(
                conn,
                task_id,
                EntryKind.NOTE,
                "Edited "
                + ", ".join(
                    [
                        *(k.replace("_", " ") for k in fields if k != "needs_title"),
                        *(["people"] if people is not None else []),
                    ]
                ),
            )
        task = store.get(conn, task_id)
    except ToddError as e:
        raise _fail(e) from e
    out.print(render.card(task, _today(), render.width(out), session.names))


@app.command("open")
def open_command(
    ctx: typer.Context,
    task_id: TaskId,
    which: Annotated[
        int | None,
        typer.Argument(help="Just this link (as numbered in todd show).", show_default=False),
    ] = None,
) -> None:
    """Open a task's links (in Slack, your browser…)."""
    try:
        task = store.get(_session(ctx).conn, task_id)
        chosen = task.links
        if which is not None:
            if not 1 <= which <= len(task.links):
                raise ToddError(
                    f"#{task_id} has {render.plural(len(task.links), 'link')}; "
                    f"there's no link {which}."
                )
            chosen = [task.links[which - 1]]
        urls = [link.url for link in chosen if link.url]
        if not urls:
            raise ToddError(f"#{task_id} has no links to open.")
        for url in urls:
            system.open_url(url)
            err.print(Text.assemble(("↗ ", render.ACCENT), (url, render.FAINT)))
    except ToddError as e:
        raise _fail(e) from e


# ── Changing state ───────────────────────────────────────────────────────────


def _refresh_tickets(session: Session, task: Task) -> None:
    """Re-read the status of this task's tickets so moves aren't planned from stale data."""
    candidates = [link for link in effects.tickets(task) if link.ref]
    if not candidates:
        return
    with err.status(render.stage("checking Jira", "Syncing"), spinner_style=render.ACCENT):
        found = triage.gather(candidates, session.config)
    assert task.id is not None
    triage.save_lookups(session.conn, task.id, [g for g in found if g.error is None])


def _push_jira(session: Session, task: Task, state: State, *, yes: bool) -> None:
    assert task.id is not None
    config, conn = session.config, session.conn
    moves = effects.jira_moves(task, state, config.jira)
    if not moves:
        return
    jira = Jira(config.jira.command, config.jira.site)
    for move in moves:
        err.print(
            Text.assemble(
                ("  Jira ", render.JIRA),
                (move.key, f"bold {render.JIRA}"),
                "  ",
                (move.link.status or "?", render.FAINT),
                " → ",
                (move.status, "bold"),
            )
        )
        # Whether a ticket should move varies too much to assume: ask about each one,
        # unless -y said to go ahead.
        if not yes:
            if not _interactive():
                render.warn(
                    err,
                    f"Left {move.key} alone: todd asks before changing Jira, and can't here.",
                    hint=f"Pass [bold]-y[/] to move it anyway, or run [bold]todd sync {task.id}[/]",
                )
                continue
            # The highlight starts on No: a ticket only moves if you pick Yes.
            if not _yes(f"Move {move.key} to {move.status}?", default=False):
                err.print(Text(f"  Left {move.key} alone.", style=render.FAINT))
                continue
        try:
            with err.status(
                render.stage(f"moving {move.key} to {move.status}", "Updating Jira"),
                spinner_style=render.JIRA,
            ):
                jira.transition(move.key, move.status)
        except ToddError as e:
            render.error(err, str(e), detail=e.detail, hint=e.hint)
            _do_it_yourself(move.link, f"Move {move.key} to {move.status} yourself:")
            continue
        assert move.link.id is not None
        with db.tx(conn):
            store.update_link(conn, move.link.id, status=move.status, fetched_at=store.now())
            store.log(conn, task.id, EntryKind.JIRA, f"{move.key} → {move.status}")
        render.success(
            err, Text.assemble((move.key, f"bold {render.JIRA}"), f" is now {move.status}")
        )


def _do_it_yourself(link: Link, what: str) -> None:
    if link.url:
        err.print(Text(f"  {what}", style=render.WARN))
        _plain(link.url)
    else:
        err.print(
            Text.assemble(
                (f"  {what} ", render.WARN),
                (link.ref or "", "bold"),
                (" (set site under [jira] so todd can link it)", render.FAINT),
            )
        )


def _reply(session: Session, task: Task, targets: list[Link], *, news: str | None = None) -> None:
    """Show where to reply in Slack, and offer to open it or draft the reply."""
    assert task.id is not None
    w = render.width(err)
    err.print()
    err.print(render.section("Reply in Slack", w, render.SLACK), width=w)
    for i, target in enumerate(targets, 1):
        err.print(render.link_block(target, i, _today(), names=session.names, width=w), width=w)
    if not _interactive():
        return
    for target in targets:
        if not target.url:
            continue
        name = f" to {target.author}" if target.author else ""
        choice = ask.choose(
            "Reply in Slack?",
            [
                ask.Option("open", "Open it"),
                ask.Option("draft", f"Draft a reply{name}"),
                ask.Option("skip", "Skip"),
            ],
            default=0,
            console=err,
        )
        if choice == "skip":
            continue
        if choice == "draft":
            try:
                with err.status(
                    render.stage("drafting a reply", "Asking Claude"), spinner_style=render.ACCENT
                ):
                    draft = effects.draft_reply(task, target, session.config, news=news)
            except ToddError as e:
                render.error(err, str(e), detail=e.detail, hint=e.hint)
                continue
            err.print(
                Panel(
                    Text(draft),
                    border_style=render.SLACK,
                    title="Draft",
                    title_align="left",
                    width=w,
                )
            )
            try:
                system.copy(draft)
                render.success(err, "Copied to your clipboard.")
            except ToddError as e:
                render.error(err, str(e), detail=e.detail)
            store.note(session.conn, task.id, "Drafted a Slack reply", EntryKind.SLACK)
            if not _yes("Open the thread to paste it?", default=True):
                continue
        try:
            system.open_url(target.url)
        except ToddError as e:
            render.error(err, str(e), detail=e.detail)
            _plain(target.url)


def _fire(session: Session, task: Task, fired: list[Followup], *, news: str | None) -> None:
    """Follow-ups that just came due: draft the message, mark it done, or keep it for later."""
    assert task.id is not None
    conn, today = session.conn, _today()
    err.print()
    for followup in fired:
        assert followup.id is not None
        err.print(
            Text.assemble(("↪ ", render.FOLLOWUP), ("Follow-up due: ", "bold"), followup.action)
        )
        choice = "later"
        if _interactive():
            choice = ask.choose(
                "Now?",
                [
                    ask.Option("draft", "Draft a message"),
                    ask.Option("told", "Told them"),
                    ask.Option("later", "Later"),
                ],
                default=2,
                console=err,
            )
        if choice == "draft":
            try:
                with err.status(
                    render.stage("drafting a message", "Asking Claude"), spinner_style=render.ACCENT
                ):
                    draft = effects.draft_followup(task, followup, session.config, news=news)
                err.print(
                    Panel(
                        Text(draft), border_style=render.FOLLOWUP, title="Draft", title_align="left"
                    )
                )
                system.copy(draft)
                render.success(err, "Copied to your clipboard.")
            except ToddError as e:
                render.error(err, str(e), detail=e.detail, hint=e.hint)
            choice = "told" if _yes("Mark it done?", default=True) else "later"
        if choice == "told":
            store.close_followup(conn, followup.id, FollowupStatus.DONE)
            render.success(err, Text("Follow-up done.", style=render.FAINT))
        else:
            store.reschedule_followup(conn, followup.id, today)
            err.print(
                Text(f"  Kept for today: it's in your queue as ↪{followup.id}.", style=render.FAINT)
            )


def _transition(
    ctx: typer.Context,
    task_id: int,
    state: State,
    words: list[str] | None,
    *,
    yes: bool,
    local: bool,
    no_jira: bool = False,
    waiting_on: str | None = None,
) -> None:
    session = _session(ctx)
    note_text = " ".join(words or []).strip() or None
    if yes and (no_jira or local):
        raise _fail(
            ToddError(
                "-y says to change Jira and --no-jira (or --local) says not to.",
                hint="Pick one, or neither to be asked about each ticket.",
            )
        )
    try:
        conn = session.conn
        before = store.get(conn, task_id)
        if before.state == state and not (state == State.WAITING and waiting_on):
            render.warn(err, f"#{task_id} is already {state.label}.")
            return
        old = store.set_state(
            conn,
            task_id,
            state,
            note=None if state == State.WAITING else note_text,
            waiting_on=waiting_on,
        )
        task = store.get(conn, task_id)
        render.success(
            out,
            Text.assemble(
                (f"#{task_id} ", "bold"),
                render.state_word(old),
                (" → ", render.FAINT),
                render.state_word(state),
                "  ",
                (task.title, render.FAINT),
            ),
        )
        if state == State.WAITING and task.waiting_on:
            out.print(Text(f"  waiting on {task.waiting_on}", style=render.WARN))
        jira = session.config.jira
        wants_jira = any(
            link.ref and jira.target(link.ref, state) for link in effects.tickets(task)
        )
        if wants_jira and not (local or no_jira):
            _refresh_tickets(session, task)
            task = store.get(conn, task_id)
            _push_jira(session, task, state, yes=yes)
        elif wants_jira:
            err.print(Text("  Left Jira alone.", style=render.FAINT))
        changes = effects.followups_on_move(task, old, state)
        for followup in changes.moot:
            assert followup.id is not None
            store.close_followup(
                conn, followup.id, FollowupStatus.DROPPED, why=f"reached {state.label}"
            )
            err.print(Text(f"  ↪ No longer needed: {followup.action}", style=render.FAINT))
        if changes.fired:
            _fire(session, store.get(conn, task_id), changes.fired, news=note_text)
        if local:
            return
        if targets := effects.slack_prompt(task, state, session.config):
            _reply(session, store.get(conn, task_id), targets, news=note_text)
    except ToddError as e:
        raise _fail(e) from e


@app.command()
def start(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Start working on a task."""
    _transition(ctx, task_id, State.DOING, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def wait(
    ctx: typer.Context,
    task_id: TaskId,
    words: Annotated[
        list[str] | None,
        typer.Argument(
            help="Who or what you're waiting on, e.g. Priya to confirm the numbers.",
            show_default=False,
        ),
    ] = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Park a task while you wait on someone or something."""
    on = " ".join(words or []).strip() or None
    _transition(
        ctx, task_id, State.WAITING, None, yes=yes, local=local, no_jira=no_jira, waiting_on=on
    )


@app.command()
def follow(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Keep an eye on something rather than do it (see them with [bold]todd following[/])."""
    _transition(ctx, task_id, State.FOLLOWING, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def move(
    ctx: typer.Context,
    task_id: TaskId,
    to: Annotated[
        str,
        typer.Argument(
            help="todo, doing, waiting, in_review, done, dropped, following or inbox.",
            show_default=False,
        ),
    ],
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Move a task to any state; todd asks before changing a Jira ticket."""
    state = parse_state(to)
    if state is None:
        raise _fail(
            ToddError(
                f"{to!r} isn't a state.",
                hint="States: " + ", ".join(s.value for s in State),
            )
        )
    _transition(ctx, task_id, state, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def review(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Mark a task as in review."""
    _transition(ctx, task_id, State.IN_REVIEW, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def done(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Finish a task: then Jira, any follow-ups, and replying in Slack."""
    _transition(ctx, task_id, State.DONE, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def drop(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Decide not to do a task."""
    _transition(ctx, task_id, State.DROPPED, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def reopen(
    ctx: typer.Context,
    task_id: TaskId,
    words: Words = None,
    yes: Yes = False,
    no_jira: NoJira = False,
    local: Local = False,
) -> None:
    """Put a task back on your to-do list."""
    _transition(ctx, task_id, State.TODO, words, yes=yes, local=local, no_jira=no_jira)


@app.command()
def reply(ctx: typer.Context, task_id: TaskId) -> None:
    """Where to reply in Slack about a task, with an offer to draft the reply."""
    session = _session(ctx)
    try:
        task = store.get(session.conn, task_id)
        targets = effects.reply_targets(task, any_slack=True)
        if not targets:
            raise ToddError(
                f"#{task_id} has no Slack links.",
                hint=f"Add one with [bold]todd link {task_id} https://…slack.com/archives/…[/]",
            )
        _reply(session, task, targets)
    except ToddError as e:
        raise _fail(e) from e


@app.command()
def sync(ctx: typer.Context, task_id: TaskId, yes: Yes = False) -> None:
    """Bring a task's Jira ticket in line with its todd state."""
    session = _session(ctx)
    try:
        task = store.get(session.conn, task_id)
        if not effects.tickets(task):
            raise ToddError(f"#{task_id} has no Jira ticket to sync.")
        _refresh_tickets(session, task)
        task = store.get(session.conn, task_id)
        if not effects.jira_moves(task, task.state, session.config.jira):
            render.success(err, "Jira already matches.")
            return
        _push_jira(session, task, task.state, yes=yes)
    except ToddError as e:
        raise _fail(e) from e


# ── Following, links, roles, refreshing ──────────────────────────────────────


@app.command()
def following(ctx: typer.Context) -> None:
    """What you're keeping an eye on, soonest check-in first."""
    session = _session(ctx)
    try:
        tasks = store.tasks(session.conn, [State.FOLLOWING])
    except ToddError as e:
        raise _fail(e) from e
    render.following(out, tasks, today=_today(), now=store.now(), names=session.names)


@app.command("links")
def links_command(
    ctx: typer.Context,
    task_ids: Annotated[list[int], typer.Argument(help="One or more tasks.", show_default=False)],
    labels: Annotated[
        bool, typer.Option("--labels", "-L", help="Put what each link is on the line above it.")
    ] = False,
) -> None:
    """Print a task's links, one per line and nothing else, ready to click or copy."""
    try:
        conn = _session(ctx).conn
        tasks = [store.get(conn, task_id) for task_id in task_ids]
    except ToddError as e:
        raise _fail(e) from e
    for task in tasks:
        for link in task.links:
            if labels:
                print(
                    f"# #{task.id} {linking.label(link)}"
                    + (f": {link.title}" if link.title else "")
                )
            print(link.target)


@app.command()
def role(
    ctx: typer.Context,
    task_id: TaskId,
    which: Annotated[int, typer.Argument(help="The link's number (see todd show).", min=1)],
    what: Annotated[
        str,
        typer.Argument(
            help="respond (reply here), source, ticket, deliverable, reference, or none.",
            show_default=False,
        ),
    ],
) -> None:
    """Say what a link is for, e.g. mark a Slack thread as where to reply. Refiling keeps it."""
    session = _session(ctx)
    try:
        conn = session.conn
        task = store.get(conn, task_id)
        if not 1 <= which <= len(task.links):
            raise ToddError(
                f"#{task_id} has {render.plural(len(task.links), 'link')}; there's no link {which}."
            )
        choice = what.strip().lower().replace("reply", "respond").replace("-", "_")
        new_role = None if choice in ("none", "") else Role(choice) if choice in Role else None
        if new_role is None and choice not in ("none", ""):
            raise ToddError(
                f"{what!r} isn't a role.",
                hint="Roles: " + ", ".join(r.value for r in Role) + ", or none.",
            )
        link = task.links[which - 1]
        assert link.id is not None
        with db.tx(conn):
            store.update_link(conn, link.id, role=new_role, role_fixed=new_role is not None)
            label = new_role.label if new_role else "no particular role"
            store.log(conn, task_id, EntryKind.NOTE, f"Link {which} is {label}")
    except ToddError as e:
        raise _fail(e) from e
    render.success(
        err,
        Text.assemble(
            (linking.label(link), "bold"),
            f" is {new_role.label if new_role else 'no particular role'} on #{task_id}",
        ),
    )


@app.command()
def refresh(
    ctx: typer.Context,
    task_id: Annotated[
        int | None,
        typer.Argument(help="Just this task. Default: everything open.", show_default=False),
    ] = None,
) -> None:
    """Re-read Jira tickets and pull requests (status, reviewers, stacks) without Claude."""
    session = _session(ctx)
    try:
        conn = session.conn
        if task_id is not None:
            targets = [store.get(conn, task_id)]
        else:
            open_states = [s for s in State if not s.closed]
            targets = [t for t in store.tasks(conn, open_states) if t.links]
        w = render.width(err)
        for task in targets:
            assert task.id is not None
            readable = [
                link for link in task.links if link.kind in (LinkKind.JIRA, LinkKind.GITHUB)
            ]
            if not readable:
                continue
            err.print(Text.assemble(("◇ ", render.ACCENT), (f"#{task.id} ", "bold"), task.title))
            with err.status(render.stage("", "Refreshing"), spinner_style=render.ACCENT) as status:
                found = triage.gather(
                    readable,
                    session.config,
                    on_step=lambda step: status.update(render.stage(step, "Refreshing")),
                    names=session.names,
                    known=task.links,
                )
            _print_lookups(session, found, w)
            triage.save_lookups(conn, task.id, found)
    except ToddError as e:
        raise _fail(e) from e


# ── Follow-ups ───────────────────────────────────────────────────────────────

followups_app = typer.Typer(
    help="Things you owe people: tell them, ask them, check in with them.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(followups_app, name="followup")
app.add_typer(followups_app, name="fu", hidden=True)


@followups_app.callback()
def followups_list(ctx: typer.Context) -> None:
    """Every open follow-up: what's due, what's coming, and what waits on a task's state."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        conn = _session(ctx).conn
        items = _with_tasks(conn, store.open_followups(conn))
    except ToddError as e:
        raise _fail(e) from e
    render.followups(out, items, today=_today())


FollowupId = Annotated[int, typer.Argument(help="The follow-up's number (↪N).", min=1)]


@followups_app.command("add")
def followup_add(
    ctx: typer.Context,
    task_id: TaskId,
    action: Annotated[list[str], typer.Argument(help="What to do, e.g. Tell Theo it's done.")],
    person: Annotated[
        str | None, typer.Option("--to", help="Who it's with.", show_default=False)
    ] = None,
    on: Annotated[
        str | None,
        typer.Option("--on", help="A date: YYYY-MM-DD, today, tomorrow, fri…", show_default=False),
    ] = None,
    when: Annotated[
        str | None,
        typer.Option("--when", help="Due when the task reaches this state.", show_default=False),
    ] = None,
    unless: Annotated[
        str | None,
        typer.Option(
            "--unless", help="Not needed if the task reaches this state first.", show_default=False
        ),
    ] = None,
) -> None:
    """Add a follow-up to a task."""
    session = _session(ctx)
    try:
        states = {}
        for name, value in (("--when", when), ("--unless", unless)):
            if value is not None:
                states[name] = parse_state(value)
                if states[name] is None:
                    raise ToddError(f"{name} {value!r} isn't a state.")
        followup = Followup(
            action=" ".join(action).strip(),
            person=person,
            due=parse_due(on, _today()) if on else None,
            when=states.get("--when"),
            unless=states.get("--unless"),
            by_you=True,
        )
        store.add_followup(session.conn, task_id, followup)
    except ToddError as e:
        raise _fail(e) from e
    render.success(err, render.followup_line(followup, _today()))


def _close_followup(ctx: typer.Context, followup_id: int, status: FollowupStatus) -> None:
    try:
        followup = store.close_followup(_session(ctx).conn, followup_id, status)
    except ToddError as e:
        raise _fail(e) from e
    render.success(err, render.followup_line(followup, _today()))


@followups_app.command("done")
def followup_done(ctx: typer.Context, followup_id: FollowupId) -> None:
    """You did it: told them, asked them, checked in."""
    _close_followup(ctx, followup_id, FollowupStatus.DONE)


@followups_app.command("drop")
def followup_drop(ctx: typer.Context, followup_id: FollowupId) -> None:
    """It's no longer needed."""
    _close_followup(ctx, followup_id, FollowupStatus.DROPPED)


@followups_app.command("snooze")
def followup_snooze(
    ctx: typer.Context,
    followup_id: FollowupId,
    on: Annotated[str, typer.Argument(help="The new date: YYYY-MM-DD, tomorrow, fri, +7…")],
) -> None:
    """Move a follow-up to another day (a check-in you'll do again later, say)."""
    try:
        conn = _session(ctx).conn
        store.reschedule_followup(conn, followup_id, parse_due(on, _today()))
        followup = store.get_followup(conn, followup_id)
    except ToddError as e:
        raise _fail(e) from e
    render.success(err, render.followup_line(followup, _today()))


# ── Setup ────────────────────────────────────────────────────────────────────


@app.command("nick")
def nick(
    ctx: typer.Context,
    login: Annotated[
        str | None,
        typer.Argument(help="A GitHub login (or org/team).", show_default=False),
    ] = None,
    name: Annotated[
        list[str] | None,
        typer.Argument(help="What you call them.", show_default=False),
    ] = None,
    remove: Annotated[bool, typer.Option("--remove", "-r", help="Forget this nickname.")] = False,
) -> None:
    """Nicknames for people, by GitHub login: todd (and Claude) will use the name you give.

    [bold]todd nick[/] lists them, [bold]todd nick priya-n Priya[/] sets one,
    [bold]todd nick priya-n --remove[/] forgets it.
    """
    names = _session(ctx).names
    try:
        if login is None:
            if not names.items():
                err.print(
                    Text.assemble(
                        ("No nicknames yet. ", render.FAINT), ("todd nick priya-n Priya", "bold")
                    )
                )
                return
            for key, value in names.items():
                out.print(Text.assemble((f"@{key}", render.FAINT), "  ", (value, "bold")))
            return
        if remove:
            names.set(login, None)
            render.success(err, f"Forgot the nickname for @{login.lstrip('@')}.")
            return
        if not name:
            current = names.get(login)
            out.print(current or Text(f"@{login.lstrip('@')} has no nickname.", style=render.FAINT))
            return
        names.set(login, " ".join(name))
        render.success(err, Text.assemble(f"@{login.lstrip('@')} is ", (" ".join(name), "bold")))
    except ToddError as e:
        raise _fail(e) from e


@app.command("config")
def config_command(
    ctx: typer.Context,
    init: Annotated[bool, typer.Option("--init", help="Write a starter config file.")] = False,
    edit_it: Annotated[
        bool, typer.Option("--edit", "-e", help="Open the config in your editor.")
    ] = False,
) -> None:
    """Show where todd keeps things and what it's configured to do."""
    session = _session(ctx)
    path = session.config_path
    try:
        if init or edit_it:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(configuration.TEMPLATE, encoding="utf-8")
                render.success(err, f"Wrote {path}")
            elif init:
                render.warn(err, f"{path} already exists; left it alone.")
            if edit_it:
                system.edit_file(path)
            return
        config = session.config
    except ToddError as e:
        raise _fail(e) from e
    faint = render.FAINT
    out.print()
    out.print(
        Text.assemble(
            ("Config    ", faint),
            str(path),
            ("" if config.loaded else "  (not created yet: todd config --init)", faint),
        )
    )
    out.print(Text.assemble(("Database  ", faint), str(session.db_path)))
    nicknames = session.names
    out.print(
        Text.assemble(
            ("Nicknames ", faint),
            str(nicknames.path),
            (f"  ({render.plural(len(nicknames.items()), 'person', 'people')}; todd nick)", faint),
        )
    )
    out.print()
    c = config.claude
    out.print(
        Text.assemble(
            ("Claude    ", faint),
            f"{c.command} -p",
            (
                f"  model {c.model or 'default'} · effort {c.effort or 'default'}"
                f" · timeout {c.timeout:g}s",
                faint,
            ),
        )
    )
    j = config.jira
    out.print(
        Text.assemble(
            ("Jira      ", faint),
            j.command,
            (
                f"  site {j.site or '(not set)'}"
                " · asks before each ticket change (-y / --no-jira to skip asking)",
                faint,
            ),
        )
    )
    for state in State:
        if state == State.INBOX:
            continue
        status = j.status.get(state)
        overrides = [
            f"{p}: {m[state] or 'leave alone'}" for p, m in j.projects.items() if state in m
        ]
        if status or overrides:
            line = Text.assemble(
                ("          ", faint),
                render.state_word(state),
                (" → ", faint),
                status or "(leave alone)",
            )
            if overrides:
                line.append("  (" + ", ".join(overrides) + ")", style=faint)
            out.print(line)
    if j.known_keys:
        out.print(
            Text.assemble(
                ("          ", faint), ("spots keys for ", faint), ", ".join(sorted(j.known_keys))
            )
        )
    s = config.slack
    out.print(
        Text.assemble(
            ("Slack     ", faint),
            "asks for message text" if s.ask_for_text else "doesn't ask for message text",
            (" · offers to reply on " + ", ".join(sorted(st.label for st in s.prompt_on)), faint),
        )
    )
    if config.projects:
        out.print()
        for name, hint in config.projects.items():
            out.print(
                Text.assemble(
                    ("Project   ", faint),
                    (name, render.ACCENT),
                    (f"  {hint}" if hint else "", faint),
                )
            )
    out.print()


def _check(label: str, ok: bool, detail: str = "", hint: str | None = None) -> None:
    out.print(
        Text.assemble(
            ("✓ " if ok else "✗ ", f"bold {render.OK if ok else render.ERROR}"),
            (f"{label:<12}", "bold"),
            (detail, render.FAINT),
        )
    )
    if hint and not ok:
        out.print(Text.from_markup(f"  {hint}"))


@app.command()
def doctor(
    ctx: typer.Context,
    jira_key: Annotated[
        str | None,
        typer.Option(
            "--jira", help="Read this ticket and show what todd makes of it.", show_default=False
        ),
    ] = None,
    ask_claude: Annotated[
        bool,
        typer.Option(
            "--claude", help="Make one tiny Claude call to check structured answers work."
        ),
    ] = False,
    pr_url: Annotated[
        str | None,
        typer.Option(
            "--pr",
            help="Read this pull request (and its stack) and show what todd makes of it.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Check that Claude Code, acli and gh are ready for todd."""
    from todd import proc

    session = _session(ctx)
    out.print()
    try:
        config = session.config
        _check(
            "config",
            True,
            str(session.config_path) + ("" if config.loaded else " (defaults; none written yet)"),
        )
    except ToddError as e:
        _check("config", False, str(e), e.hint)
        config = Config()
    try:
        conn = session.conn
        n = conn.execute("SELECT count(*) FROM task").fetchone()[0]
        _check(
            "database",
            True,
            f"{session.db_path} · {render.plural(n, 'task')} · schema v{db.current_version(conn)}",
        )
    except (ToddError, sqlite3.Error) as e:
        _check("database", False, str(e))

    def version(command: str) -> tuple[bool, str]:
        try:
            result = proc.run([command, "--version"], timeout=20)
        except proc.ProcError as e:
            return False, str(e)
        said = (result.stdout.strip() or result.stderr.strip()).splitlines()
        return result.ok, said[0] if said else ""

    ok, detail = version(config.claude.command)
    _check("claude", ok, detail, "Install Claude Code: https://code.claude.com")
    if ok and ask_claude:
        try:
            started = datetime.now()
            with err.status(
                render.stage("one tiny structured call", "Asking Claude"),
                spinner_style=render.ACCENT,
            ):
                answer = Claude(config.claude).structured(
                    "Reply with ok set to true.",
                    system="You are a health check. Answer in the requested structure.",
                    schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                )
            took = (datetime.now() - started).total_seconds()
            _check("claude -p", answer.get("ok") is True, f"structured answers work · {took:.1f}s")
        except ToddError as e:
            _check("claude -p", False, str(e) + (f" ({e.detail})" if e.detail else ""), e.hint)

    ok, detail = version(config.jira.command)
    _check(
        "acli", ok, detail, "Install the Atlassian CLI: https://developer.atlassian.com/cloud/acli/"
    )
    if ok:
        try:
            result = Jira(config.jira.command, config.jira.site).auth_status()
            said = " ".join((result.stdout or result.stderr).split())[:100]
            _check("jira auth", result.ok, said, "Sign in with [bold]acli jira auth login[/]")
        except ToddError as e:
            _check("jira auth", False, str(e), e.hint)
    if ok and jira_key:
        try:
            ticket = Jira(config.jira.command, config.jira.site).view(jira_key.upper())
            _check(
                ticket.key,
                True,
                f"{ticket.summary!r} · {ticket.status} · {ticket.type}"
                f" · assignee {ticket.assignee}",
            )
            if ticket.description:
                first = " ".join(ticket.description.split())[:100]
                out.print(Text(f"  description: {first}…", style=render.FAINT))
            out.print(
                Text(
                    f"  url: {ticket.url or '(set site under [jira] to link bare keys)'}",
                    style=render.FAINT,
                )
            )
        except ToddError as e:
            _check(jira_key, False, str(e) + (f" · {e.detail}" if e.detail else ""), e.hint)

    ok, detail = version("gh")
    _check("gh", ok, detail + ("" if ok else " (optional: pull requests and their stacks)"))
    if ok and pr_url:
        _doctor_pr(pr_url, session.names)
    out.print()


def _doctor_pr(url: str, names: Nicknames) -> None:
    from todd import github

    try:
        context = github.pull_request(url)
    except ToddError as e:
        _check("pull request", False, str(e) + (f" · {e.detail}" if e.detail else ""), e.hint)
        return
    if context.stack_error:
        _check(
            "stack", False, f"couldn't check: {context.stack_error} ({context.stack_error.detail})"
        )
    elif context.stack:
        stack = context.stack
        _check(
            "stack",
            True,
            f"#{stack.number} in {stack.owner}/{stack.repo} · "
            f"{len(stack.numbers)} pull requests onto {stack.trunk} · "
            f"{'open' if stack.open else 'closed'}",
        )
    else:
        _check("stack", True, "not stacked")
    for pr in context.pulls:
        where = f"{pr.stack_position}. " if pr.stack_position else ""
        reviews = ", ".join(
            f"{'you' if r.you else names.name(r.login)} {r.state.label}" for r in pr.reviewers
        )
        out.print(
            Text.assemble(
                (f"  {where}", render.FAINT),
                (f"#{pr.number} ", "bold"),
                (pr.title or "", ""),
                (f" · {pr.status} · by {names.name(pr.author)}", render.FAINT),
            )
        )
        detail = [f"reviews: {reviews or 'none'}"]
        if pr.open_threads:
            detail.append(f"{render.plural(len(pr.open_threads), 'open thread')}")
        if pr.comments:
            detail.append(render.plural(len(pr.comments), "comment"))
        out.print(Text("     " + " · ".join(detail), style=render.FAINT))


# ── How `todd --help` lists the commands ────────────────────────────────────

# Headings in the order they appear, each with its commands in order: what you do first
# (capture), then looking at things, working tasks through their states, the outside tools,
# the people side, and setup. (Typer lists a command group like followup after the plain
# commands under the same heading.)
HELP_LAYOUT = {
    "Capture and edit": ["add", "link", "note", "edit", "role", "triage"],
    "Look": ["ls", "show", "following", "links", "open"],
    "Move": ["start", "wait", "review", "done", "follow", "drop", "reopen", "move"],
    "Jira, GitHub and Slack": ["reply", "sync", "refresh"],
    "People and follow-ups": ["nick", "followup"],
    "Setup": ["config", "doctor"],
}


def command_name(info: typer.models.CommandInfo) -> str:
    return info.name or getattr(info.callback, "__name__", "").replace("_", "-")


def _lay_out_help() -> None:
    order = [name for names in HELP_LAYOUT.values() for name in names]
    heading = {name: title for title, names in HELP_LAYOUT.items() for name in names}
    app.registered_commands.sort(key=lambda info: order.index(command_name(info)))
    for info in app.registered_commands:
        info.rich_help_panel = heading[command_name(info)]
    for group in app.registered_groups:
        if group.name in heading:
            group.rich_help_panel = heading[group.name]


_lay_out_help()


def main() -> None:
    app()
