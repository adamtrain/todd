"""`todd`: capture work tasks, let Claude file them, and keep Jira in step as you work them."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, TextIO

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from typer.core import TyperGroup

from todd import __version__, ask, db, effects, intent, render, store, system, triage
from todd import capture as capturing
from todd import config as configuration
from todd import links as linking
from todd import pull as pulling
from todd.claude import Claude
from todd.config import Config
from todd.errors import ToddError
from todd.jira import Jira
from todd.models import (
    FROM_FOLLOWING,
    EntryKind,
    Followup,
    FollowupStatus,
    Link,
    LinkKind,
    Priority,
    Role,
    Standing,
    State,
    Task,
    parse_state,
    standing,
)
from todd.people import Nicknames

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)


class Listening(TyperGroup):
    """Anything that isn't a command is a request in your own words, for `todd do`.

    So `todd "move the Torii task to done"` works, and so does `todd show me what's waiting
    on Nik`: "show" is a command, but "me what's waiting on Nik" doesn't fit it.
    """

    def resolve_command(self, ctx: typer.Context, args: list[str]):  # ty: ignore[invalid-method-override]
        if args and self.is_request(ctx, args):
            return "do", self.get_command(ctx, "do"), list(args)
        return super().resolve_command(ctx, args)

    def is_request(self, ctx: typer.Context, args: list[str]) -> bool:
        first = args[0]
        if first.startswith("-"):
            return False
        command = self.get_command(ctx, first)
        if command is None:
            return True
        if any(word in ("-h", "--help") for word in args[1:]):
            return False
        try:
            command.make_context(first, list(args[1:]), parent=ctx, resilient_parsing=False)
        except typer.Exit:
            return False
        except typer.TyperException:
            return True
        return False


app = typer.Typer(
    cls=Listening,
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
    # The numbers in use when this command last looked.
    numbers: store.Numbers = field(default_factory=store.Numbers)

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
            self.numbers = store.numbers(self._conn)
        return self._conn

    @property
    def opened(self) -> bool:
        """Whether this command has used the database (and so may have changed it)."""
        return self._conn is not None


def _session(ctx: typer.Context) -> Session:
    session = ctx.find_root().obj
    assert isinstance(session, Session)
    return session


class _Plan:
    """A plan's steps were written with the numbers as they were, so while there are steps
    still to come (`waiting`), numbers stay put. They settle during the last step, which
    reports against the numbers the plan `began` with."""

    waiting = False
    began: store.Numbers | None = None


def _settle(session: Session, *, final: bool = False) -> store.Renumbered:
    """Bring numbers back down after things were closed or added, and say what moved.

    Open things are numbered from 1 with no gaps, so finishing or dropping one frees its
    number for those after it. A command settles when it ends (`final`), which is also when
    numbers it showed for anything new get reported; `add` settles sooner, so that what it
    shows is already final.
    """
    if _Plan.waiting or not session.opened:
        return store.Renumbered()
    before, began = session.numbers, _Plan.began or store.Numbers()
    try:
        moved = store.renumber(session.conn)
    except (ToddError, sqlite3.Error) as e:
        render.warn(err, f"Couldn't renumber things: {e}")
        return store.Renumbered()
    session.numbers, _Plan.began = moved.now, None

    def worth_saying(
        changes: dict[int, int], known: frozenset[int], was: frozenset[int], now: frozenset[int]
    ) -> dict[int, int]:
        # Things that are open, or were until this command closed them. Closed things
        # shuffling along behind aren't news, and nor is a number nobody was shown.
        return {
            old: new
            for old, new in changes.items()
            if (old in was or new in now) and (old in known or final)
        }

    tasks = worth_saying(
        moved.tasks,
        before.tasks | began.tasks,
        before.open_tasks | began.open_tasks,
        moved.now.open_tasks,
    )
    followups = worth_saying(
        moved.followups,
        before.followups | began.followups,
        before.open_followups | began.open_followups,
        moved.now.open_followups,
    )
    if (notice := render.renumbered(tasks, followups)) is not None:
        err.print(notice)
    return moved


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
    '  [cyan]todd "move the Torii task to done and start the next one"[/]\n'
    '  [cyan]todd "what am I waiting on from Nik?"[/]\n'
    '  [cyan]todd add "reply to Priya re: Q3 numbers" https://acme.slack.com/archives/D…/p…[/]\n'
    "  [cyan]todd[/]                     what you can act on now, with follow-ups that are due\n"
    "  [cyan]todd ls[/]                  everything open, by project\n"
    "  [cyan]todd states[/]              every state and what it means\n"
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

    Say what you want in your own words, [bold]todd "…"[/], and Claude works out the commands
    below (you see the plan first). With nothing at all, shows what you can act on now.
    """
    session = ctx.obj = Session(db.db_path(db_file), db.config_path(config_file))
    ctx.call_on_close(lambda: _settle(session, final=True))
    if ctx.invoked_subcommand is None:
        _now(session)


def _projects(conn: sqlite3.Connection, *, closed: bool = False) -> list[tuple[Task, list[Task]]]:
    """Each project with its tasks in order. Finished and dropped ones only with `closed`."""
    found = []
    for project in store.tasks(conn, projects=True):
        assert project.id is not None
        tasks = store.project_tasks(conn, project.id)
        where = standing(project, tasks).state
        if closed or where is None or not where.closed:
            found.append((project, tasks))
    return found


def _standing_of(conn: sqlite3.Connection, project_id: int) -> Standing:
    return standing(store.get(conn, project_id), store.project_tasks(conn, project_id))


def _matching(
    session: Session, states: list[State] | None, area: str | None, person: str | None
) -> list[Task]:
    """Tasks (not projects) in these states, narrowed to an area or a person if given."""
    who = session.names.aliases(person) if person else None
    return store.tasks(session.conn, states, area=area, person=who, projects=False)


def _now(session: Session, *, area: str | None = None, person: str | None = None) -> None:
    """What you can act on now: doing, to do and not blocked, or not filed yet."""
    try:
        conn = session.conn
        _settle(session)
        tasks = _matching(session, [s for s in State if not s.closed], area, person)
        free = [t for t in tasks if not t.blocked]
        counts = {
            "waiting": sum(t.state == State.WAITING for t in free),
            "in review": sum(t.state == State.IN_REVIEW for t in free),
            "blocked": sum(t.blocked for t in tasks),
            "following": sum(t.state == State.FOLLOWING for t in free),
        }
        local = datetime.now().astimezone()
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0)
        filtered = bool(area or person)
        due = [] if filtered else _with_tasks(conn, store.open_followups(conn), due_only=True)
        done = store.closed_since(conn, monday)
    except ToddError as e:
        raise _fail(e) from e
    render.now_view(
        out,
        [t for t in free if t.state in render.NOW_ORDER],
        today=_today(),
        now=store.now(),
        due=due,
        counts=counts,
        done_this_week=done,
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


Area = Annotated[
    str | None, typer.Option("--area", help="Only this area of work.", show_default=False)
]
Person = Annotated[
    str | None,
    typer.Option(
        "--person",
        help="Only tasks involving this person (a name, or a GitHub login).",
        show_default=False,
    ),
]


@app.command()
def now(ctx: typer.Context, area: Area = None, person: Person = None) -> None:
    """What you can act on now: doing, next and not blocked, and follow-ups that are due.

    Anything not filed yet is here too. Each task says which project it's part of. What's
    waiting, in review, blocked or only followed is counted underneath: see [bold]todd ls[/].
    """
    _now(_session(ctx), area=area, person=person)


@app.command("ls")
def list_tasks(
    ctx: typer.Context,
    everything: Annotated[
        bool, typer.Option("--all", "-a", help="Include done and dropped tasks and projects.")
    ] = False,
    following: Annotated[
        bool, typer.Option("--following", "-f", help="Include what you're following.")
    ] = False,
    area: Area = None,
    person: Person = None,
) -> None:
    """Everything open, by project, then the tasks that aren't part of one.

    Each project shows its state (which comes from its tasks) and its tasks in order, blocked
    ones too, with what blocks them.
    """
    session = _session(ctx)
    try:
        conn = session.conn
        _settle(session)
        states = None if everything else [s for s in State if not s.closed]
        wanted = {t.id: t for t in _matching(session, states, area, person)}
        sections = []
        for project, tasks in _projects(conn, closed=everything):
            shown = [t for t in tasks if t.id in wanted]
            sections.append(render.Section(project.title, shown, project=project, all_tasks=tasks))
        alone = [t for t in wanted.values() if t.project_id is None]
        on_your_list = [t for t in alone if t.state != State.FOLLOWING]
        sections.append(render.Section("No project", render.by_state(on_your_list)))
        if following or everything:
            followed = [t for t in alone if t.state == State.FOLLOWING]
            color = render.STATE_COLORS[State.FOLLOWING]
            sections.append(render.Section("Following", followed, color=color))
    except ToddError as e:
        raise _fail(e) from e
    render.overview(out, sections, today=_today(), now=store.now(), names=session.names)


@app.command()
def states() -> None:
    """Every state and what it means; how projects, blocking and following work."""
    render.glossary(out)


@app.command()
def show(ctx: typer.Context, task_id: TaskId) -> None:
    """Everything about a task (or a project and its tasks): links, messages, timeline."""
    session = _session(ctx)
    try:
        task = store.get(session.conn, task_id)
        tasks = store.project_tasks(session.conn, task_id) if task.is_project else None
    except ToddError as e:
        raise _fail(e) from e
    render.show(out, task, today=_today(), names=session.names, tasks=tasks)


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


def _file(
    session: Session,
    task: Task,
    *,
    label: str = "Filing",
    as_project: bool = False,
    review: bool = False,
) -> Task | None:
    """Look up the task's links, then have Claude file it. Returns the task (or the project
    it became) as saved.

    With `review`, you see what Claude would file first, and can add it, ask Claude to
    change it (as often as you like), or leave things as they were (then this returns None).
    """
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
    links = [g.link for g in found]

    def ask_claude(step: str, **revising) -> triage.Filing:
        with err.status(render.stage(step, label), spinner_style=render.ACCENT):
            return triage.ask(
                task,
                found,
                config,
                used_areas=store.areas(conn),
                today=_today(),
                names=session.names,
                as_project=as_project,
                **revising,
            )

    filing = ask_claude("asking Claude")
    changes: list[str] = []
    while review:
        fresh = triage.splittable(task)
        owners = triage.owners(filing, links) if filing.is_project else None
        render.preview(
            err,
            filing,
            links,
            today=_today(),
            state=None if fresh else task.state,
            owners=owners,
            names=session.names,
        )
        choice = ask.choose(
            "File it?",
            [
                ask.Option("add", "Add it"),
                ask.Option("change", "Change it…"),
                ask.Option("leave", "Leave it in the inbox" if fresh else "Keep it as it was"),
            ],
            default=0,
            console=err,
        )
        if choice == "add":
            break
        if choice == "leave":
            return None
        change = Prompt.ask(
            Text.from_markup("  What should Claude change? [dim](Enter to go back)[/]"),
            console=err,
            default="",
            show_default=False,
        ).strip()
        if change:
            changes.append(change)
            filing = ask_claude("making your changes", previous=filing, changes=changes)
    applied = triage.apply(conn, task, filing, links, as_project=as_project)
    moved = _settle(session)  # a project's new tasks, and Claude's follow-ups, get their numbers
    filed_id = moved.task(task.id)
    if _interactive():
        for task_id in [filed_id, *(moved.task(n) for n in applied.tasks)]:
            if (each := store.get(conn, task_id)).needs_title:
                _ask_for_title(session, each)
    return store.get(conn, filed_id)


def _print_filed(session: Session, filed: Task) -> None:
    w = render.width(out)
    if filed.is_project:
        assert filed.id is not None
        tasks = store.project_tasks(session.conn, filed.id)
        render.success(
            err,
            Text.assemble(
                f"Filed #{filed.id} as a project with {render.plural(len(tasks), 'task')}"
            ),
        )
        out.print(render.card(filed, _today(), w, session.names, tasks=tasks))
        out.print(render.task_table(tasks, today=_today(), w=w, names=session.names, states=True))
        return
    render.success(err, Text.assemble(f"Filed #{filed.id} in ", render.state_word(filed.state)))
    out.print(render.card(filed, _today(), w, session.names))


def _ask_for_title(session: Session, task: Task) -> None:
    """Claude couldn't tell what the task is, so ask rather than guess."""
    assert task.id is not None
    which = f"task {task.project_position} of this project" if task.project_id else "this"
    err.print(
        Text.assemble(
            ("? ", f"bold {render.WARN}"),
            f"Claude couldn't tell what {which} is from what it could read.",
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
        err.print(render.lookup_line(g.link, problem, w, names=session.names, new=g.new, via=g.via))


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
    as_project: Annotated[
        bool,
        typer.Option("--project", help="It's a project, even if its tasks aren't clear yet."),
    ] = False,
    into: Annotated[
        int | None,
        typer.Option("--in", help="Add it as a task in this project.", show_default=False),
    ] = None,
    after: Annotated[
        str | None,
        typer.Option(
            "--after",
            help="With --in: the task(s) it waits on, like 14 or 14,15, or none. "
            "Default: the project's last open task.",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="File it without showing you a preview first.")
    ] = False,
) -> None:
    """Capture a task. Claude files it: title, next step, due date, follow-ups and more.

    Describe a chain ("…once that's in, a bug bash, then configure it for Acme") and Claude
    makes a project, with a task for each part waiting on the one before.

    Mention follow-ups in your own words ("tell Theo when I'm done") and they're kept too.

    Pass links as arguments. For a Slack link, todd asks you to paste the message
    (or use [bold]-q[/]) so its text stays with the link even though todd can't read Slack.
    A pull request brings the rest of its stack, and the Jira ticket its title names in
    parentheses, like "(PLAT-412) Move the workers".
    With no arguments, todd opens your editor, or reads piped text.
    """
    session = _session(ctx)
    try:
        if into is not None and as_project:
            raise ToddError("A task can go --in a project, or be a --project, but not both.")
        if after is not None and into is None:
            raise ToddError("--after only means something with --in.")
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
        project = store.get(conn, into) if into is not None else None
        if project is not None and not project.is_project:
            raise ToddError(
                f"#{into} isn't a project.",
                hint="See your projects with [bold]todd projects[/].",
            )
        if project is not None and project.state == State.DROPPED:
            raise ToddError(
                f"Project #{into} was dropped.",
                hint=f"Bring it back first with [bold]todd reopen {into}[/].",
            )
        task = Task(
            title=_provisional_title(capture),
            description=capture.description,
            links=capture.links,
        )
        if project is not None:
            assert project.id is not None
            task.project_id = project.id
            task.project_position = store.next_position(conn, project.id)
        waits_on = _parse_after(conn, after, project) if project is not None else []
        store.add(conn, task)
        assert task.id is not None
        with db.tx(conn):
            for blocker in waits_on:
                store.add_blocker(conn, task.id, blocker)
        # A new task takes the first number after the open ones, before anything shows it.
        task = store.get(conn, _settle(session).task(task.id))
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
        filed = _file(session, task, as_project=as_project, review=_interactive() and not yes)
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
    if filed is None:
        err.print(
            Text.assemble(
                (f"#{task.id} is in your inbox, unfiled. File it later with ", render.FAINT),
                (f"todd triage {task.id}", "bold"),
            )
        )
        return
    _print_filed(session, filed)


def _parse_after(conn: sqlite3.Connection, after: str | None, project: Task) -> list[int]:
    """Which tasks a new task in `project` waits on: as given, or the last open one."""
    assert project.id is not None
    if after is None:
        open_tasks = [t for t in store.project_tasks(conn, project.id) if not t.state.closed]
        return [open_tasks[-1].id] if open_tasks and open_tasks[-1].id is not None else []
    if after.strip().lower() in ("", "none", "nothing"):
        return []
    found = []
    for part in after.replace(",", " ").split():
        try:
            number = int(part.lstrip("#"))
        except ValueError:
            raise ToddError(f"--after {after!r}: {part!r} isn't a task number.") from None
        store.get(conn, number)
        found.append(number)
    return found


@app.command("triage")
def triage_command(
    ctx: typer.Context,
    task_id: Annotated[
        int | None,
        typer.Argument(
            help="The task to (re)file. Default: everything in your inbox.", show_default=False
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="File without showing you a preview first.")
    ] = False,
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
            label = "Refiling" if task.triaged else "Filing"
            filed = _file(session, task, label=label, review=_interactive() and not yes)
            if filed is None:
                err.print(Text(f"  Left #{task.id} as it was.", style=render.FAINT))
                continue
            _print_filed(session, filed)
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


def _project_number(conn: sqlite3.Connection, text: str) -> int | None:
    """The project `--in` names, or None for "none"."""
    if text.strip().lower() in ("", "none"):
        return None
    try:
        number = int(text.strip().lstrip("#"))
    except ValueError:
        raise ToddError(f"--in {text!r} isn't a project number.") from None
    if not store.get(conn, number).is_project:
        raise ToddError(f"#{number} isn't a project.")
    return number


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
    area: Annotated[
        str | None,
        typer.Option("--area", "-a", help='An area of work, or "none".', show_default=False),
    ] = None,
    into: Annotated[
        str | None,
        typer.Option(
            "--in",
            help='Make it a task in this project (its number), or "none".',
            show_default=False,
        ),
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
        current = store.get(conn, task_id)
        fields: dict[str, object] = {}
        if title is not None:
            if not title.strip():
                raise ToddError("A title can't be empty.")
            fields["title"] = title.strip()
            fields["needs_title"] = False
        if next_action is not None:
            fields["next_action"] = next_action.strip() or None
        if area is not None:
            fields["area"] = None if area.strip().lower() in ("", "none") else area.strip().lower()
        if into is not None:
            target = _project_number(conn, into)
            if current.is_project:
                raise ToddError("A project can't be part of another project.")
            if target is not None and current.state == State.FOLLOWING:
                raise ToddError(
                    f"#{task_id} is something you're following, which stays outside projects.",
                    hint=f"Take it on first with [bold]todd reopen {task_id}[/].",
                )
            home = current.project_id
            if home not in (None, target) and len(store.project_tasks(conn, home)) == 1:
                raise ToddError(
                    f"#{task_id} is the only task in project #{home}, and a project has at "
                    "least one task.",
                    hint="Add another task to it first, or drop the project with "
                    f"[bold]todd drop {home}[/].",
                )
            if target != home:
                fields["project_id"] = target
                fields["project_position"] = (
                    store.next_position(conn, target) if target is not None else None
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
                        *(
                            k.replace("_", " ")
                            for k in fields
                            if k not in ("needs_title", "project_position")
                        ),
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
                    hint=f"Pass [bold]-y[/] to move it anyway, or run [bold]todd push {task.id}[/]",
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
                Text(f"  Kept for today: it's in todd now as ↪{followup.id}.", style=render.FAINT)
            )


def _check_move(task: Task, state: State) -> None:
    """The moves that aren't allowed: following is its own thing, outside projects."""
    if state == State.FOLLOWING and task.project_id is not None:
        raise ToddError(
            f"#{task.id} is part of project #{task.project_id}, and following is for things "
            "outside projects.",
            hint=f"Park it with [bold]todd wait {task.id}[/], or take it out of the project "
            f"with [bold]todd edit {task.id} --in none[/].",
        )
    if task.state == State.FOLLOWING and state not in (*FROM_FOLLOWING, State.FOLLOWING):
        raise ToddError(
            f"#{task.id} is something you're following: it can only become to do, done or dropped.",
            hint=f"Take it on with [bold]todd reopen {task.id}[/], then move it along.",
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
    with_project: bool = False,
) -> None:
    """Move a task, then do what follows from that. `with_project` is for a task moving
    because its whole project is: the project speaks for itself then."""
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
        if before.is_project:
            _move_project(ctx, before, state, note_text, yes=yes, local=local, no_jira=no_jira)
            return
        _check_move(before, state)
        if before.state == state and not (state == State.WAITING and waiting_on):
            render.warn(err, f"#{task_id} is already {state.label}.")
            return
        project_was = _standing_of(conn, before.project_id) if before.project_id else None
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
        if not state.closed and task.open_blockers and not with_project:
            waits = ", ".join(f"#{b.id} {b.title}" for b in task.open_blockers)
            render.warn(err, f"#{task_id} is still blocked by {waits}.")
        _after_move(session, task, old, state, note_text, yes=yes, local=local, no_jira=no_jira)
        if with_project:
            return
        _report_blocking(conn, task_id, old, state)
        if before.project_id is not None and project_was is not None:
            _project_moved(
                session,
                before.project_id,
                project_was,
                note_text,
                yes=yes,
                local=local,
                no_jira=no_jira,
            )
    except ToddError as e:
        raise _fail(e) from e


def _after_move(
    session: Session,
    task: Task,
    old: State,
    state: State,
    news: str | None,
    *,
    yes: bool,
    local: bool,
    no_jira: bool,
) -> None:
    """What follows a change of state: Jira (asked about, ticket by ticket), follow-ups that
    come due or stop mattering, and where to reply in Slack."""
    assert task.id is not None
    conn, task_id = session.conn, task.id
    jira = session.config.jira
    wants_jira = any(link.ref and jira.target(link.ref, state) for link in effects.tickets(task))
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
        _fire(session, store.get(conn, task_id), changes.fired, news=news)
    if not local and (targets := effects.slack_prompt(task, state, session.config)):
        _reply(session, store.get(conn, task_id), targets, news=news)


def _project_moved(
    session: Session,
    project_id: int,
    was: Standing,
    news: str | None,
    *,
    yes: bool,
    local: bool,
    no_jira: bool,
) -> None:
    """A task moved, so its project may stand somewhere new. Say so, and do what follows for
    the project's own ticket, follow-ups and Slack links."""
    conn = session.conn
    project = store.get(conn, project_id)
    tasks = store.project_tasks(conn, project_id)
    now = standing(project, tasks)
    if now.label == was.label:
        return
    err.print()
    line = Text.assemble(
        ("▸ ", render.PROJECT), (f"#{project_id} {project.title}", render.PROJECT), " is now "
    )
    line.append(now.label, style=f"bold {render.standing_color(now)}")
    if now.state == State.DONE:
        line.append(f": all {render.plural(len(tasks), 'task')} finished", style=render.FAINT)
    err.print(line)
    if now.state is not None:
        _after_move(
            session,
            project,
            was.state or State.TODO,
            now.state,
            news,
            yes=yes,
            local=local,
            no_jira=no_jira,
        )


def _move_project(
    ctx: typer.Context,
    project: Task,
    state: State,
    note_text: str | None,
    *,
    yes: bool,
    local: bool,
    no_jira: bool,
) -> None:
    """A project's state comes from its tasks, so the only moves it takes itself are being
    dropped (which drops its open tasks) and being brought back from that."""
    assert project.id is not None
    session = _session(ctx)
    conn = session.conn
    tasks = store.project_tasks(conn, project.id)
    dropped = project.state == State.DROPPED
    if state == State.DROPPED and not dropped:
        still = [t for t in tasks if not t.state.closed]
        if still and not yes and _interactive():
            question = f"Drop “{project.title}” and its {render.plural(len(still), 'open task')}?"
            if not _yes(question, default=False):
                err.print(Text("  Left it as it was.", style=render.FAINT))
                return
        was = standing(project, tasks)
        # The project first, so its tasks are stamped as dropped with it (see reopening).
        store.set_state(conn, project.id, State.DROPPED, note=note_text)
        render.success(
            out,
            Text.assemble(
                (f"#{project.id} ", "bold"),
                (was.label, f"bold {render.standing_color(was)}"),
                (" → ", render.FAINT),
                render.state_word(State.DROPPED),
                "  ",
                (project.title, render.FAINT),
            ),
        )
        for task in still:
            assert task.id is not None
            _transition(
                ctx,
                task.id,
                State.DROPPED,
                None,
                yes=yes,
                local=local,
                no_jira=no_jira,
                with_project=True,
            )
        _after_move(
            session,
            store.get(conn, project.id),
            was.state or State.TODO,
            State.DROPPED,
            note_text,
            yes=yes,
            local=local,
            no_jira=no_jira,
        )
        return
    if state == State.TODO and dropped:
        store.set_state(conn, project.id, State.TODO, note=note_text)
        back = [
            t
            for t in tasks
            if t.state == State.DROPPED
            and t.state_at is not None
            and project.state_at is not None
            and t.state_at >= project.state_at
        ]
        for task in back:
            assert task.id is not None
            _transition(
                ctx,
                task.id,
                State.TODO,
                None,
                yes=yes,
                local=local,
                no_jira=no_jira,
                with_project=True,
            )
        now = _standing_of(conn, project.id)
        render.success(
            out,
            Text.assemble(
                (f"#{project.id} ", "bold"),
                render.state_word(State.DROPPED),
                (" → ", render.FAINT),
                (now.label, f"bold {render.standing_color(now)}"),
                "  ",
                (project.title, render.FAINT),
            ),
        )
        return
    if state == State.DROPPED:
        render.warn(err, f"#{project.id} is already dropped.")
        return
    raise ToddError(
        f"#{project.id} is a project: its state comes from its tasks.",
        hint=f"Move one of its tasks instead (see [bold]todd show {project.id}[/]), or add one "
        f'with [bold]todd add "…" --in {project.id}[/]. A project itself can only be dropped, '
        "and reopened after that.",
    )


def _report_blocking(conn: sqlite3.Connection, task_id: int, old: State, new: State) -> None:
    """Say which tasks this move frees up (or holds up again)."""
    if new.closed == old.closed:
        return
    for dependent in store.dependents(conn, task_id):
        if dependent.state.closed:
            continue
        if new.closed and not dependent.blocked:
            err.print(
                Text.assemble(
                    ("  ▸ Unblocked: ", render.PROJECT),
                    (f"#{dependent.id} ", "bold"),
                    dependent.title,
                    "  ",
                    render.link_badges(dependent),
                )
            )
        elif not new.closed:
            err.print(
                Text(
                    f"  #{dependent.id} {dependent.title} waits on this again.", style=render.FAINT
                )
            )


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
    """Keep an eye on something rather than do it (see them with [bold]todd following[/]).

    Following is for things outside projects, and from there a task can only become to do
    ([bold]todd reopen[/]), done or dropped.
    """
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
    """Move a task to any state; todd asks before changing a Jira ticket.

    [bold]todd states[/] says what each state means.
    """
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
    """Decide not to do a task. Dropping a project drops its open tasks too."""
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
    """Put a task back on your to-do list, or take on something you were following."""
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
def push(ctx: typer.Context, task_id: TaskId, yes: Yes = False) -> None:
    """Push a task's state to Jira: move its tickets to match (asking about each)."""
    session = _session(ctx)
    try:
        task = store.get(session.conn, task_id)
        if not effects.tickets(task):
            raise ToddError(f"#{task_id} has no Jira ticket to push to.")
        _refresh_tickets(session, task)
        task = store.get(session.conn, task_id)
        if not effects.jira_moves(task, task.state, session.config.jira):
            render.success(err, "Jira already matches.")
            return
        _push_jira(session, task, task.state, yes=yes)
    except ToddError as e:
        raise _fail(e) from e


# ── Projects ─────────────────────────────────────────────────────────────────


@app.command()
def projects(
    ctx: typer.Context,
    everything: Annotated[
        bool, typer.Option("--all", "-a", help="Include finished and dropped projects.")
    ] = False,
) -> None:
    """Your projects, each with its tasks in order (finished ones too) and what blocks what."""
    session = _session(ctx)
    try:
        conn = session.conn
        _settle(session)
        items = _projects(conn, closed=everything)
    except ToddError as e:
        raise _fail(e) from e
    render.projects(out, items, today=_today(), names=session.names)


@app.command()
def block(
    ctx: typer.Context,
    task_id: TaskId,
    on: Annotated[
        list[int], typer.Option("--on", help="A task it can't start before. Repeat for more.")
    ],
) -> None:
    """Say a task can't start until another one is done.

    Several tasks can wait on the same one (and so run at the same time), and one task can wait
    on several.
    """
    try:
        conn = _session(ctx).conn
        task = store.get(conn, task_id)
        blockers = [store.get(conn, number) for number in on]
        with db.tx(conn):
            for blocker in blockers:
                assert blocker.id is not None
                store.add_blocker(conn, task_id, blocker.id)
                store.log(conn, task_id, EntryKind.NOTE, f"Waits on “{blocker.title}”")
    except ToddError as e:
        raise _fail(e) from e
    names = ", ".join(f"#{b.id} {b.title}" for b in blockers)
    render.success(err, Text.assemble((f"#{task.id} ", "bold"), f"waits on {names}"))


@app.command()
def unblock(
    ctx: typer.Context,
    task_id: TaskId,
    on: Annotated[
        int | None,
        typer.Option("--on", help="Only stop waiting on this task.", show_default=False),
    ] = None,
) -> None:
    """Let a task start without waiting (on one task, or on anything)."""
    try:
        conn = _session(ctx).conn
        store.get(conn, task_id)
        other = store.get(conn, on) if on is not None else None
        with db.tx(conn):
            removed = store.remove_blockers(conn, task_id, on)
            if removed:
                what = f"“{other.title}”" if other is not None else "anything"
                store.log(conn, task_id, EntryKind.NOTE, f"No longer waits on {what}")
    except ToddError as e:
        raise _fail(e) from e
    if not removed:
        render.warn(err, f"#{task_id} wasn't waiting on {'#' + str(on) if on else 'anything'}.")
        return
    render.success(err, f"#{task_id} no longer waits on {'#' + str(on) if on else 'anything'}.")


# ── Following, links, roles, refreshing ──────────────────────────────────────


@app.command()
def following(ctx: typer.Context) -> None:
    """What you're keeping an eye on, soonest check-in first."""
    session = _session(ctx)
    try:
        conn = session.conn
        _settle(session)
        tasks = store.tasks(conn, [State.FOLLOWING], projects=False)
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
def pull(
    ctx: typer.Context,
    task_id: Annotated[
        int | None,
        typer.Argument(
            help="A task, or a project (with its tasks). Default: everything open.",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Make the updates Claude proposes without asking.")
    ] = False,
    links_only: Annotated[
        bool, typer.Option("--links-only", help="Just re-read the links; don't ask Claude.")
    ] = False,
) -> None:
    """Pull from Jira and GitHub: re-read a task's links and update the task to match."""
    session = _session(ctx)
    try:
        conn = session.conn
        if task_id is not None:
            first = store.get(conn, task_id)
            targets = [first]
            if first.is_project:
                targets += [t for t in store.project_tasks(conn, task_id) if not t.state.closed]
        else:
            open_states = [s for s in State if not s.closed]
            targets = [
                t
                for t in store.tasks(conn, open_states)
                if pulling.readable(t) and t.state != State.DROPPED
            ]
        news = [
            _pull_one(ctx, session, task, asked=task_id is not None, yes=yes, links_only=links_only)
            for task in targets
        ]
        if task_id is None and not any(news):
            render.success(err, "Everything's up to date.")
    except ToddError as e:
        raise _fail(e) from e


def _pull_one(
    ctx: typer.Context,
    session: Session,
    task: Task,
    *,
    asked: bool,
    yes: bool,
    links_only: bool,
) -> bool:
    """Re-read one task's links, then (if anything's new, or you asked about this task) see
    what Claude proposes. Returns whether anything was new."""
    assert task.id is not None
    conn, config = session.conn, session.config
    links = pulling.readable(task)
    if not links and not asked:
        return False
    err.print(Text.assemble(("◇ ", render.ACCENT), (f"#{task.id} ", "bold"), task.title))
    found: list[triage.Gathered] = []
    if links:
        before = pulling.snapshot(task)
        with err.status(render.stage("", "Pulling"), spinner_style=render.ACCENT) as status:
            found = triage.gather(
                links,
                config,
                on_step=lambda step: status.update(render.stage(step, "Pulling")),
                names=session.names,
                known=task.links,
            )
        triage.save_lookups(conn, task.id, found)
        w = render.width(err)
        for g in found:
            g.changed = pulling.changes(before.get(g.link.ref or g.link.url or ""), g.link)
            if g.changed or g.error:
                problem = str(g.error) if g.error else None
                err.print(
                    render.lookup_line(
                        g.link, problem, w, names=session.names, new=g.new, via=g.via
                    )
                )
                for change in g.changed:
                    err.print(Text(f"      {change}", style=render.FAINT))
    new = any(g.changed for g in found)
    if not new:
        err.print(Text("  Nothing new on its links.", style=render.FAINT))
    # A project's state comes from its tasks, and a followed item isn't yours to move along.
    links_only = links_only or task.is_project or task.state == State.FOLLOWING
    if links_only or (not new and not asked):
        return new
    task = store.get(conn, task.id)

    def ask_claude(step: str, **revising) -> pulling.Update:
        with err.status(render.stage(step, "Pulling"), spinner_style=render.ACCENT):
            return pulling.ask(task, found, config, today=_today(), names=session.names, **revising)

    update = ask_claude("asking Claude")
    requests: list[str] = []
    while True:
        diff = update.changes_to(task)
        if not diff:
            err.print(Text(f"  #{task.id} is up to date.", style=render.FAINT))
            return new
        render.update_preview(err, task, update, diff)
        if yes:
            break
        if not _interactive():
            render.warn(
                err, "Left it as it was.", hint=f"Apply it with [bold]todd pull {task.id} -y[/]"
            )
            return new
        choice = ask.choose(
            "Update it?",
            [
                ask.Option("apply", "Apply it"),
                ask.Option("change", "Change it…"),
                ask.Option("skip", "Skip"),
            ],
            default=0,
            console=err,
        )
        if choice == "skip":
            return new
        if choice == "apply":
            break
        request = Prompt.ask(
            Text.from_markup("  What should Claude change? [dim](Enter to go back)[/]"),
            console=err,
            default="",
            show_default=False,
        ).strip()
        if request:
            requests.append(request)
            update = ask_claude("making your changes", previous=update, requests=requests)
    _apply_update(ctx, session, task, update, diff)
    return True


def _apply_update(
    ctx: typer.Context,
    session: Session,
    task: Task,
    update: pulling.Update,
    diff: dict[str, tuple[str | None, str | None]],
) -> None:
    """Write a pulled update. A state change goes through the usual move, so Jira is still
    asked about, follow-ups fire and blocked tasks hear about it."""
    assert task.id is not None
    conn = session.conn
    with db.tx(conn):
        if "next" in diff:
            store.update(conn, task.id, next_action=update.next_action)
        if "waiting on" in diff and "state" not in diff:
            store.update(conn, task.id, waiting_on=update.waiting_on)
        store.log(conn, task.id, EntryKind.NOTE, f"Pulled: {update.reason or 'updated'}")
    if "state" in diff:
        waiting_on = update.waiting_on if update.state == State.WAITING else None
        _transition(ctx, task.id, update.state, None, yes=False, local=False, waiting_on=waiting_on)
    else:
        render.success(err, Text.assemble((f"#{task.id} ", "bold"), "updated"))


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
    session = _session(ctx)
    try:
        conn = session.conn
        _settle(session)
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
        conn = session.conn
        added = store.add_followup(conn, task_id, followup)
        followup = store.get_followup(conn, _settle(session).followup(added))
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
        session = _session(ctx)
        conn = session.conn
        store.reschedule_followup(conn, followup_id, parse_due(on, _today()))
        # Snoozing one that was closed opens it again, which gives it an open number.
        followup = store.get_followup(conn, _settle(session).followup(followup_id))
    except ToddError as e:
        raise _fail(e) from e
    render.success(err, render.followup_line(followup, _today()))


# ── In your own words ────────────────────────────────────────────────────────


@app.command("do", hidden=True)
def do(
    ctx: typer.Context,
    words: Annotated[list[str], typer.Argument(help="What you want, in your own words.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Run the plan without asking first.")
    ] = False,
) -> None:
    """Say what you want: Claude works out the todd commands, shows you, and runs them."""
    session = _session(ctx)
    request = " ".join(words).strip()
    group = command_group()
    conversation = intent.Conversation(request)
    try:
        while True:
            with err.status(
                render.stage("working out what you mean", "Asking Claude"),
                spinner_style=render.ACCENT,
            ):
                plan = intent.understand(
                    conversation,
                    group,
                    session.conn,
                    session.config,
                    today=_today(),
                    names=session.names,
                )
            if plan.question:
                err.print(Text.assemble(("? ", f"bold {render.ACCENT}"), plan.question))
                if not _interactive():
                    raise typer.Exit(1)
                answer = Prompt.ask("  ", console=err, default="", show_default=False).strip()
                if not answer:
                    err.print(Text("Nothing done.", style=render.FAINT))
                    return
                conversation.exchanges.append((plan.question, answer))
                continue
            if problems := intent.problems_in(plan, group):
                raise ToddError(
                    "Claude's plan has commands todd can't run, so nothing was done.",
                    detail="\n".join(problems),
                    hint="Try saying it another way, or use a command directly (todd --help).",
                )
            if not plan.steps:
                render.warn(err, "Claude didn't find anything to do for that.")
                return
            render.plan(err, plan.steps)
            if plan.looks_only or yes:
                break
            if not _interactive():
                render.warn(
                    err,
                    "Nothing done: todd asks before changing anything, and can't ask here.",
                    hint='Run it in a terminal, or say [bold]todd do -y "…"[/].',
                )
                raise typer.Exit(1)
            choice = ask.choose(
                "Do it?",
                [
                    ask.Option("do", "Do it"),
                    ask.Option("change", "Change it…"),
                    ask.Option("cancel", "Cancel"),
                ],
                default=0,
                console=err,
            )
            if choice == "cancel":
                err.print(Text("Nothing done.", style=render.FAINT))
                return
            if choice == "do":
                break
            change = Prompt.ask(
                Text.from_markup("  What should change? [dim](Enter to go back)[/]"),
                console=err,
                default="",
                show_default=False,
            ).strip()
            if change:
                conversation.previous = plan
                conversation.changes.append(change)
    except ToddError as e:
        raise _fail(e) from e
    _run_plan(session, group, plan.steps)


def command_group() -> TyperGroup:
    """todd's commands, as the CLI sees them: for planning and running a request's steps."""
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    return group


def _run_plan(session: Session, group: TyperGroup, steps: list[intent.Step]) -> None:
    """Run each step through the ordinary command, stopping at the first that doesn't finish."""
    base = ["--db", str(session.db_path), "--config", str(session.config_path)]
    _Plan.began = session.numbers
    for i, step in enumerate(steps, 1):
        if len(steps) > 1:
            err.print(Text.assemble((f"▶ {i}/{len(steps)} ", render.ACCENT), (step.says, "bold")))
        # The steps name tasks by the numbers they had when the plan was made, so nothing is
        # renumbered while there are steps to come. The last one settles as it would alone.
        _Plan.waiting = i < len(steps)
        try:
            code = group.main(args=[*base, *step.argv], prog_name="todd", standalone_mode=False)
        except typer.Abort:
            code = 130
        except typer.TyperException as e:
            render.error(err, intent.message(e))
            code = 1
        finally:
            _Plan.waiting = False
        if isinstance(code, int) and code != 0:
            if i < len(steps):
                render.warn(
                    err, f"Stopped after step {i}: it didn't finish, so the rest wasn't run."
                )
            raise typer.Exit(code)


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
    if config.areas:
        out.print()
        for name, hint in config.areas.items():
            out.print(
                Text.assemble(
                    ("Area      ", faint),
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
        if key := linking.ticket_in_title(pr.title):
            detail.append(f"ticket in its title: {key}")
        out.print(Text("     " + " · ".join(detail), style=render.FAINT))


# ── How `todd --help` lists the commands ────────────────────────────────────

# Headings in the order they appear, each with its commands in order: what you do first
# (capture), then looking at things, working tasks through their states, the outside tools,
# the people side, and setup. (Typer lists a command group like followup after the plain
# commands under the same heading.)
HELP_LAYOUT = {
    "Capture and edit": ["add", "link", "note", "edit", "role", "triage"],
    "Look": ["now", "ls", "projects", "following", "show", "links", "open", "states"],
    "Move": ["start", "wait", "review", "done", "follow", "drop", "reopen", "move"],
    "Blocking": ["block", "unblock"],
    "Jira, GitHub and Slack": ["reply", "push", "pull"],
    "People and follow-ups": ["nick", "followup"],
    "Setup": ["config", "doctor"],
}


def command_name(info: typer.models.CommandInfo) -> str:
    return info.name or getattr(info.callback, "__name__", "").replace("_", "-")


def _lay_out_help() -> None:
    order = [name for names in HELP_LAYOUT.values() for name in names]
    heading = {name: title for title, names in HELP_LAYOUT.items() for name in names}
    # Hidden commands (like `do`, which `todd "…"` reaches) aren't listed, so they go last.
    app.registered_commands.sort(
        key=lambda info: order.index(name) if (name := command_name(info)) in order else len(order)
    )
    for info in app.registered_commands:
        if (name := command_name(info)) in heading:
            info.rich_help_panel = heading[name]
    for group in app.registered_groups:
        if group.name in heading:
            group.rich_help_panel = heading[group.name]


_lay_out_help()


def main() -> None:
    app()
