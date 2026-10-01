"""Everything todd prints: lists, task cards, capture progress, and errors."""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from todd import links as linking
from todd import reviews
from todd.capture import Capture
from todd.models import (
    Entry,
    EntryKind,
    Followup,
    Link,
    LinkKind,
    Priority,
    ReviewState,
    Standing,
    State,
    Task,
    TaskRef,
    standing,
)
from todd.people import Nicknames

NO_NAMES = Nicknames()

ACCENT = "#7c83f7"
FAINT = "grey42"
ERROR = "#f2506e"
OK = "#1fbf8f"
WARN = "#eb9a12"
JIRA = "#4c9aff"
SLACK = "#36c5f0"

STATE_COLORS = {
    State.INBOX: "grey62",
    State.TODO: ACCENT,
    State.DOING: OK,
    State.WAITING: WARN,
    State.IN_REVIEW: "#a871f7",
    State.DONE: "#3d8f6f",
    State.DROPPED: FAINT,
    State.FOLLOWING: "#5fa8d3",
}
FOLLOWUP = "#e8a0bf"
PROJECT = "#d7a65f"
DEFERRED = "#8fa3b8"

# What you can act on, in the order `todd now` shows it: what you're on, then what's next.
NOW_ORDER = [State.DOING, State.TODO, State.INBOX]
# Every open state, most active first: how tasks outside a project are ordered in `todd ls`.
OPEN_ORDER = [State.DOING, State.IN_REVIEW, State.TODO, State.WAITING, State.INBOX]
CLOSED_ORDER = [State.DONE, State.DROPPED]
# The links column in lists is as wide as its widest entry, up to this; longer ones wrap.
LINKS_WIDTH = 40
# …and only sits beside the title when that leaves the title at least this much room.
TITLE_WIDTH = 44

PRIORITY_MARKS = {
    Priority.URGENT: ("‼", f"bold {ERROR}"),
    Priority.HIGH: ("!", f"bold {WARN}"),
    Priority.NORMAL: (" ", ""),
    Priority.LOW: ("↓", FAINT),
}


def width(console: Console) -> int:
    """The whole terminal: lists use all of it, and wrap rather than cut things off."""
    return console.width


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return ""
    seconds = max(0, int((now - moment).total_seconds()))
    for size, unit in ((86400 * 30, "mo"), (86400 * 7, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"


def when(moment: datetime, today: date) -> str:
    """A local timestamp, as briefly as it can be said."""
    local = moment.astimezone()
    if local.date() == today:
        return f"{local:%H:%M}"
    if local.year == today.year:
        return f"{local:%b} {local.day} {local:%H:%M}"
    return f"{local:%b} {local.day} {local.year}"


def due_text(due: date | None, today: date) -> Text:
    if due is None:
        return Text("")
    days = (due - today).days
    if days < 0:
        return Text(f"overdue {-days}d", style=f"bold {ERROR}")
    if days == 0:
        return Text("today", style=f"bold {WARN}")
    if days == 1:
        return Text("tomorrow", style=WARN)
    if days < 7:
        return Text(f"{due:%a}")
    if due.year == today.year:
        return Text(f"{due:%b} {due.day}")
    return Text(f"{due:%b} {due.day} {due.year}")


def state_badge(state: State) -> Text:
    return Text(f" {state.label.upper()} ", style=f"bold #111111 on {STATE_COLORS[state]}")


def standing_color(where: Standing) -> str:
    if where.state is not None:
        return STATE_COLORS[where.state]
    return DEFERRED if where.until else PROJECT


def until_text(day: date, today: date) -> str:
    """A date something is put off until, the way lists say dates: "Fri", "Oct 12"."""
    return f"until {due_text(day, today).plain}"


def standing_words(where: Standing, today: date) -> str:
    """A project's state in words: "waiting", "deferred until Oct 12"."""
    return f"{where.label} {until_text(where.until, today)}" if where.until else where.label


def standing_badge(where: Standing) -> Text:
    return Text(f" {where.label.upper()} ", style=f"bold #111111 on {standing_color(where)}")


def state_word(state: State) -> Text:
    return Text(state.label, style=f"bold {STATE_COLORS[state]}")


def link_badges(task: Task) -> Text:
    """A compact summary of a task's links for lists. It wraps rather than being cut off."""
    text = Text()
    parts: list[Text] = []
    for link in task.links_of(LinkKind.JIRA):
        parts.append(Text(link.ref or "Jira", style=JIRA))
    slack = task.links_of(LinkKind.SLACK)
    if slack:
        parts.append(Text("Slack" + (f" ({len(slack)})" if len(slack) > 1 else ""), style=SLACK))
    stacks: dict[str, int] = {}
    for link in task.links_of(LinkKind.GITHUB):
        if link.stack:
            stacks[link.stack] = stacks.get(link.stack, 0) + 1
        else:
            parts.append(Text("#" + (link.ref or "").rsplit("#", 1)[-1]))
    parts += [
        Text(f"stack {key.rpartition('/')[2]} ({plural(n, 'PR')})") for key, n in stacks.items()
    ]
    other = task.links_of(LinkKind.URL)
    if other:
        parts.append(Text("link" + (f"s ({len(other)})" if len(other) > 1 else ""), style=FAINT))
    for i, part in enumerate(parts):
        if i:
            text.append(" · ", style=FAINT)
        text.append_text(part)
    return text


def error(console: Console, message: str, *, detail: str | None = None, hint: str | None = None):
    console.print(Text.assemble(("✗ ", f"bold {ERROR}"), (message, "bold")))
    if detail:
        for line in detail.strip().splitlines():
            console.print(Text(f"  {line}", style=FAINT))
    if hint:
        console.print(Text.from_markup(f"  {hint}"))


def warn(console: Console, message: str, *, hint: str | None = None) -> None:
    console.print(Text.assemble(("! ", f"bold {WARN}"), message))
    if hint:
        console.print(Text.from_markup(f"  {hint}"))


def success(console: Console, message: Text | str) -> None:
    console.print(Text.assemble(("✓ ", f"bold {OK}"), message))


def stage(step: str, doing: str = "Filing") -> Text:
    return Text.assemble((doing, ACCENT), (f" · {step}", FAINT) if step else "")


def section(title: str, w: int, color: str = FAINT) -> Rule:
    return Rule(Text(f" {title} ", style=color), align="left", style=FAINT, characters="─")


# ── Follow-ups and reviews ───────────────────────────────────────────────────


def trigger_text(followup: Followup, today: date) -> Text:
    """When a follow-up is due: "when done", "Fri, unless it's in review by then", "anytime"."""
    if followup.when:
        return Text(f"when {followup.when.label}", style=FAINT)
    if followup.due:
        text = due_text(followup.due, today)
        if not text.plain.startswith(("overdue", f"{followup.due:%b} ")):
            text.append(f" {followup.due:%b} {followup.due.day}", style=FAINT)
        if followup.unless:
            text.append(f", unless it's {followup.unless.label} by then", style=FAINT)
        return text
    return Text("anytime", style=FAINT)


def followup_line(followup: Followup, today: date, *, number: bool = True) -> Text:
    closed = not followup.open
    text = Text()
    if number:
        text.append(f"↪{followup.id} ", style=FAINT if closed else FOLLOWUP)
    else:
        text.append("↪ ", style=FAINT if closed else FOLLOWUP)
    text.append(followup.action, style=f"strike {FAINT}" if closed else "")
    if closed:
        text.append(f"  {followup.status.value}", style=FAINT)
    else:
        text.append("  ")
        text.append_text(trigger_text(followup, today))
    return text


def waiting_summary(task: Task, names: Nicknames, *, limit: int = 3) -> str | None:
    """Whom a task's open pull requests are waiting on for review, most-waited-on first."""
    tally = reviews.board(task.links)
    if not tally.waiting:
        return None
    ranked = sorted(
        tally.waiting.items(),
        key=lambda item: (-len(item[1]), (names.name(item[0]) or item[0]).lower()),
    )
    shown = [
        f"{names.name(login)}" + (f" ({len(prs)})" if len(prs) > 1 else "")
        for login, prs in ranked[:limit]
    ]
    more = len(ranked) - limit
    return ", ".join(shown) + (f" +{more} more" if more > 0 else "")


_REVIEW_MARKS = {
    ReviewState.APPROVED: ("✓", OK),
    ReviewState.CHANGES_REQUESTED: ("✗", ERROR),
    ReviewState.REQUESTED: ("…", WARN),
    ReviewState.COMMENTED: ("·", FAINT),
    ReviewState.DISMISSED: ("·", FAINT),
}


def review_table(task: Task, names: Nicknames) -> Table | None:
    """One row per pull request: its state, then each reviewer marked ✓, ✗ or … (waiting)."""
    pulls = [link for link in task.links_of(LinkKind.GITHUB) if link.reviewers]
    if not pulls:
        return None
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, style="bold")
    grid.add_column(no_wrap=True, style=FAINT)
    grid.add_column()
    order = {state: i for i, state in enumerate(_REVIEW_MARKS)}
    for link in sorted(pulls, key=lambda link: (link.stack or "", link.stack_position or 0)):
        who = Text()
        for i, r in enumerate(sorted(link.reviewers, key=lambda r: order[r.state])):
            mark, style = _REVIEW_MARKS[r.state]
            if i:
                who.append("  ")
            who.append(f"{mark} ", style=style)
            who.append("you" if r.you else (names.name(r.login) or r.login))
        grid.add_row(reviews.number(link), link.status or "", who)
    return grid


# ── Lists ────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Section:
    """One group in `todd ls`: a project with its tasks, or tasks that share a heading."""

    title: str
    tasks: list[Task]  # the ones shown
    project: Task | None = None
    all_tasks: list[Task] = field(default_factory=list)  # a project's state comes from all of these
    color: str = ""


def _sort_key(task: Task) -> tuple:
    due = task.due.toordinal() if task.due else 10**7
    stamp = task.state_at.timestamp() if task.state_at else 0
    match task.state:
        case State.TODO | State.DOING:
            return (task.priority.rank, due, task.id or 0)
        case State.WAITING | State.IN_REVIEW:
            return (stamp, task.id or 0)
        case State.DONE | State.DROPPED:
            return (-stamp, -(task.id or 0))
    return (task.id or 0,)


def by_state(tasks: Iterable[Task]) -> list[Task]:
    """Tasks with the most active first: doing, in review, to do, waiting… then closed ones."""
    order = [*OPEN_ORDER, State.FOLLOWING, *CLOSED_ORDER]
    return sorted(tasks, key=lambda t: (order.index(t.state), _sort_key(t)))


def _detail(task: Task, names: Nicknames, today: date) -> Text | None:
    """The line under a task's title: what it's stuck on, or what to do next."""
    if task.state == State.INBOX:
        return Text.assemble(
            ("not filed yet · ", FAINT), (f"todd triage {task.id}", f"{FAINT} bold")
        )
    if task.needs_title:
        return Text.assemble(
            ("needs a title · ", WARN), (f'todd edit {task.id} -t "…"', f"{FAINT} bold")
        )
    if task.blocked and not task.state.closed:
        waits = ", ".join(f"#{b.id} {b.title}" for b in task.open_blockers)
        line = Text(f"blocked by {waits}", style=PROJECT)
        if task.defer_until and task.deferred(today):
            line.append(f" · deferred {until_text(task.defer_until, today)}", style=DEFERRED)
        return line
    if task.defer_until and task.deferred(today):
        return Text(f"deferred {until_text(task.defer_until, today)}", style=DEFERRED)
    if task.state == State.WAITING and (who := waiting_summary(task, names)):
        return Text(f"waiting on reviews from {who}", style=WARN)
    if task.state == State.WAITING and task.waiting_on:
        return Text(f"waiting on {task.waiting_on}", style=WARN)
    if task.next_action and not task.state.closed:
        return Text(f"→ {task.next_action}", style=FAINT)
    return None


def state_text(task: Task, today: date) -> Text:
    """A task's state as a word; "blocked" or "deferred" when it can't start yet."""
    if task.blocked and not task.state.closed:
        return Text("blocked", style=PROJECT)
    if task.deferred(today):
        return Text("deferred", style=DEFERRED)
    return Text(task.state.label, style=STATE_COLORS[task.state])


@dataclass(frozen=True, slots=True)
class Columns:
    """How wide the narrow columns are, shared by every table in one listing so that its
    sections line up. Each is as wide as its widest entry; links wrap past LINKS_WIDTH."""

    number: int = 3
    due: int = 0
    links: int = 0
    age: int = 0

    @classmethod
    def fitting(cls, tasks: Iterable[Task], today: date, now: datetime | None = None) -> Columns:
        tasks = list(tasks)

        def widest(cells: Iterable[int]) -> int:
            return max(cells, default=0)

        return cls(
            number=widest(len(f"#{t.id}") for t in tasks),
            due=widest(_due(t, today).cell_len for t in tasks),
            links=min(LINKS_WIDTH, widest(link_badges(t).cell_len for t in tasks)),
            age=widest(len(ago(t.state_at, now)) for t in tasks) if now else 0,
        )


def _due(task: Task, today: date) -> Text:
    """A task's deadline as lists show it: "due Fri", "due today", "overdue 2d"."""
    when = due_text(None if task.state.closed else task.due, today)
    if not when.plain or when.plain.startswith("overdue"):
        return when
    return Text.assemble(("due ", FAINT), when)


def task_table(
    tasks: list[Task],
    *,
    today: date,
    w: int,
    now: datetime | None = None,
    names: Nicknames = NO_NAMES,
    project_names: bool = False,
    states: bool = False,
    columns: Columns | None = None,
) -> Table:
    """Tasks as rows. `project_names` says which project each belongs to (for flat lists);
    `states` shows each task's place in its project and its state (for lists by project).

    Nothing is cut off: titles wrap, and on a narrow terminal each task's links go on a line
    under its title rather than in a column beside it.
    """
    columns = columns or Columns.fitting(tasks, today, now)
    state_width = len(State.IN_REVIEW.label)
    fixed = [1, columns.number, columns.due, columns.links]
    fixed += [2, state_width] if states else []
    fixed += [columns.age] if now is not None else []
    beside = w - sum(fixed) - len(fixed) >= TITLE_WIDTH  # room for links beside the title?
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    if states:
        table.add_column(justify="right", style=FAINT, no_wrap=True, width=2)
    table.add_column(no_wrap=True, width=1)
    table.add_column(justify="right", style=FAINT, no_wrap=True, width=columns.number)
    table.add_column(ratio=1)  # the title takes what's left, and wraps
    if states:
        table.add_column(no_wrap=True, width=state_width)
    table.add_column(no_wrap=True, width=columns.due)
    if beside:
        table.add_column(width=columns.links)
    if now is not None:
        table.add_column(no_wrap=True, justify="right", style=FAINT, width=columns.age)
    for task in tasks:
        mark, mark_style = PRIORITY_MARKS[task.priority]
        closed = task.state.closed
        title = Text(task.title, style="strike " + FAINT if task.state == State.DROPPED else "")
        if closed and task.state != State.DROPPED:
            title.stylize(FAINT)
        if states and task.priority != Priority.NORMAL and not closed:
            title = Text.assemble((f"{mark} ", mark_style), title)
        if project_names and task.project:
            title.append(f"  ▸ {task.project.title}", style=PROJECT)
        lines = [title]
        if (detail := _detail(task, names, today)) is not None:
            lines.append(detail)
        badges = link_badges(task)
        if not beside and badges.plain:
            lines.append(badges)
        row: list[RenderableType] = []
        if states:
            row.append(str(task.project_position or ""))
        row.append(task_mark(task, today) if states else Text(mark, style=mark_style))
        row += [f"#{task.id}", Group(*lines) if len(lines) > 1 else title]
        if states:
            row.append(state_text(task, today))
        row.append(_due(task, today))
        if beside:
            row.append(badges)
        if now is not None:
            row.append(ago(task.state_at, now))
        table.add_row(*row)
    return table


def _due_followups(items: list[tuple[Followup, Task]], today: date, w: int) -> Table:
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    table.add_column(justify="right", style=FOLLOWUP, no_wrap=True, min_width=5)
    table.add_column(ratio=1)
    table.add_column(no_wrap=True)
    for followup, task in items:
        about = Text(f"#{task.id} {task.title}", style=FAINT)
        table.add_row(
            f"↪{followup.id}", Group(Text(followup.action), about), trigger_text(followup, today)
        )
    return table


def _elsewhere(counts: dict[str, int], done_this_week: int) -> Text:
    """The footer of `todd now`: what's open but not yours to act on, and where to see it."""
    styles = {
        "waiting": STATE_COLORS[State.WAITING],
        "in review": STATE_COLORS[State.IN_REVIEW],
        "blocked": PROJECT,
        "deferred": DEFERRED,
        "following": STATE_COLORS[State.FOLLOWING],
    }
    footer = Text()
    for name, style in styles.items():
        if counts.get(name):
            if footer.plain:
                footer.append(" · ", style=FAINT)
            footer.append(str(counts[name]), style=f"bold {style}")
            footer.append(f" {name}", style=FAINT)
    if footer.plain:
        footer = Text.assemble(("Not yours to act on now: ", FAINT), footer)
    if done_this_week:
        footer.append(" · " if footer.plain else "", style=FAINT)
        footer.append(f"✓ {done_this_week} done this week", style=STATE_COLORS[State.DONE])
    if footer.plain:
        footer.append("\ntodd ls", style=f"{FAINT} bold")
        footer.append(" shows everything, by project", style=FAINT)
    return footer


def now_view(
    console: Console,
    tasks: list[Task],
    *,
    today: date,
    now: datetime,
    due: list[tuple[Followup, Task]] | None = None,
    counts: dict[str, int] | None = None,
    done_this_week: int = 0,
    names: Nicknames = NO_NAMES,
) -> None:
    """What you can act on now: follow-ups that are due, what you're doing, what's next (and
    not blocked), and anything not filed yet. Each task says which project it's part of."""
    w = width(console)
    columns = Columns.fitting(tasks, today, now)
    groups = {
        state: sorted((t for t in tasks if t.state == state), key=_sort_key) for state in NOW_ORDER
    }
    console.print()
    if due:
        console.print(
            Text.assemble(
                ("↪ ", FOLLOWUP), ("Follow-ups due", f"bold {FOLLOWUP}"), (f" {len(due)}", FAINT)
            )
        )
        console.print(_due_followups(due, today, w))
        console.print()
    if not any(groups.values()) and not due:
        console.print(Text("Nothing to act on right now.", style="bold"))
        console.print(
            Text.assemble(
                ("Add something: ", FAINT),
                ('todd "remind me to reply to Priya about the migration"', f"bold {ACCENT}"),
            )
        )
        console.print()
    for state, group in groups.items():
        if not group:
            continue
        color = STATE_COLORS[state]
        console.print(
            Text.assemble(
                ("● ", color),
                (state.label.capitalize(), f"bold {color}"),
                (f" {len(group)}", FAINT),
            )
        )
        table = task_table(
            group, today=today, now=now, w=w, names=names, project_names=True, columns=columns
        )
        console.print(table)
        console.print()
    footer = _elsewhere(counts or {}, done_this_week)
    if footer.plain:
        console.print(footer)
        console.print()


def overview(
    console: Console,
    sections: list[Section],
    *,
    today: date,
    now: datetime,
    names: Nicknames = NO_NAMES,
) -> None:
    """Everything open, by project: each project with its state and its tasks in order, then
    the tasks that aren't part of a project."""
    w = width(console)
    columns = Columns.fitting((t for section in sections for t in section.tasks), today, now)
    console.print()
    if not any(section.tasks for section in sections):
        console.print(Text("Nothing open.", style="bold"))
        console.print(
            Text.assemble(
                ("Add something: ", FAINT),
                ('todd "remind me to reply to Priya about the migration"', f"bold {ACCENT}"),
            )
        )
        console.print()
        return
    for section in sections:
        if not section.tasks:
            continue
        if section.project is not None:
            where = standing(section.project, section.all_tasks, today)
            head = Text.assemble(
                ("▸ ", PROJECT),
                (f"#{section.project.id} ", FAINT),
                (section.project.title, f"bold {PROJECT}"),
                "  ",
                (standing_words(where, today), f"bold {standing_color(where)}"),
                (f" · {progress(section.all_tasks)}", FAINT),
            )
        else:
            color = section.color or "default"
            head = Text.assemble(
                ("▸ ", color), (section.title, f"bold {color}"), (f" {len(section.tasks)}", FAINT)
            )
        console.print(head, width=w)
        table = task_table(
            section.tasks, today=today, now=now, w=w - 2, names=names, states=True, columns=columns
        )
        console.print(Padding(table, (0, 0, 0, 2)), width=w)
        console.print()


def glossary(console: Console) -> None:
    """Every state and what it means, so you know what to ask for."""
    from todd import glossary as words

    w = width(console)

    def paragraph(text: str) -> Padding:
        return Padding(Text.from_markup(_code(text)), (0, 0, 0, 4))

    console.print()
    console.print(section("Task states", w), width=w)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for state, meaning in words.STATES:
        grid.add_row(state_word(state), Text.from_markup(_code(meaning)))
    console.print(Padding(grid, (0, 0, 0, 4)), width=w)
    for title, text, color in (
        ("Blocked", words.BLOCKED, PROJECT),
        ("Deferred", words.DEFERRED, DEFERRED),
        ("Projects", words.PROJECTS, PROJECT),
        ("Following", words.FOLLOWING, STATE_COLORS[State.FOLLOWING]),
        ("Follow-ups", words.FOLLOW_UPS, FOLLOWUP),
        ("Numbers", words.NUMBERS, FAINT),
    ):
        console.print()
        console.print(section(title, w, color), width=w)
        console.print(paragraph(text), width=w)
    console.print()
    console.print(section("What a link is for", w), width=w)
    roles = Table.grid(padding=(0, 2))
    roles.add_column(no_wrap=True, style="bold")
    roles.add_column()
    for role, meaning in words.ROLES:
        roles.add_row(role.value, meaning)
    console.print(Padding(roles, (0, 0, 0, 4)), width=w)
    console.print()


def _code(text: str) -> str:
    """Backticked commands in glossary text, shown bold."""
    parts = text.replace("[", "\\[").split("`")
    return "".join(f"[bold]{part}[/]" if i % 2 else part for i, part in enumerate(parts))


def following(
    console: Console, tasks: list[Task], *, today: date, now: datetime, names: Nicknames = NO_NAMES
) -> None:
    """What you're keeping an eye on, soonest check-in first."""
    w = width(console)
    console.print()
    if not tasks:
        console.print(Text("You're not following anything.", style="bold"))
        console.print(
            Text.assemble(
                ("Follow something: ", FAINT),
                ('todd add "Following the X refactor, might land on me" ', f"bold {ACCENT}"),
                ("or ", FAINT),
                ("todd follow 12", f"bold {ACCENT}"),
            )
        )
        console.print()
        return

    def next_check(task: Task) -> Followup | None:
        dated = [f for f in task.open_followups if f.due]
        return min(dated, key=lambda f: f.due or today) if dated else None

    far = date.max.toordinal()
    tasks = sorted(
        tasks, key=lambda t: c.due.toordinal() if (c := next_check(t)) and c.due else far
    )
    color = STATE_COLORS[State.FOLLOWING]
    console.print(
        Text.assemble(("● ", color), ("Following", f"bold {color}"), (f" {len(tasks)}", FAINT))
    )
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    table.add_column(justify="right", style=FAINT, no_wrap=True, min_width=3)
    table.add_column(ratio=1)
    table.add_column(max_width=LINKS_WIDTH)
    table.add_column(no_wrap=True, justify="right", style=FAINT)
    for task in tasks:
        body: list[RenderableType] = [Text(task.title)]
        if (check := next_check(task)) is not None:
            line = Text.assemble(("↪ ", FOLLOWUP), check.action, "  ")
            line.append_text(trigger_text(check, today))
            body.append(line)
        table.add_row(f"#{task.id}", Group(*body), link_badges(task), ago(task.state_at, now))
    console.print(table)
    console.print()


def followups(console: Console, items: list[tuple[Followup, Task]], *, today: date) -> None:
    """Every open follow-up: what's due, what's coming, and what waits on a task's state."""
    w = width(console)
    console.print()
    if not items:
        console.print(Text("No follow-ups.", style="bold"))
        console.print(
            Text.assemble(
                ("Add one: ", FAINT),
                ('todd followup add 12 "Tell Theo it\'s done" --when done', f"bold {ACCENT}"),
            )
        )
        console.print()
        return
    groups = {
        "Due": [(f, t) for f, t in items if f.is_due(today)],
        "Coming up": [(f, t) for f, t in items if f.due and not f.is_due(today) and not f.when],
        "When the task moves": [(f, t) for f, t in items if f.when],
        "Whenever": [(f, t) for f, t in items if not f.due and not f.when],
    }
    for title, group in groups.items():
        if not group:
            continue
        style = f"bold {FOLLOWUP}" if title == "Due" else "bold"
        console.print(Text.assemble((title, style), (f" {len(group)}", FAINT)))
        console.print(_due_followups(group, today, w))
        console.print()


# ── One task ─────────────────────────────────────────────────────────────────


def facts(task: Task, today: date) -> Text:
    """area · priority · due, whichever are known."""
    parts: list[Text] = []
    if task.area:
        parts.append(Text(task.area, style=ACCENT))
    if task.priority != Priority.NORMAL:
        mark, style = PRIORITY_MARKS[task.priority]
        parts.append(Text(f"{mark} {task.priority.value}".strip(), style=style))
    if task.due:
        relative = due_text(task.due, today)
        due = relative if relative.plain.startswith("overdue") else Text.assemble("due ", relative)
        if not relative.plain.startswith(f"{task.due:%b} "):
            due.append(f" ({task.due:%b} {task.due.day})", style=FAINT)
        if task.due_hint:
            due.append(f" “{task.due_hint}”", style=f"italic {FAINT}")
        parts.append(due)
    elif task.due_hint:
        parts.append(Text(f"“{task.due_hint}”", style=f"italic {FAINT}"))
    text = Text()
    for i, part in enumerate(parts):
        if i:
            text.append(" · ", style=FAINT)
        text.append_text(part)
    return text


def card(
    task: Task,
    today: date,
    w: int,
    names: Nicknames = NO_NAMES,
    *,
    heading: str | None = None,
    tasks: list[Task] | None = None,
) -> Panel:
    """A task's card. For a project, pass its `tasks`: its state comes from theirs."""
    color = STATE_COLORS[task.state]
    badge = state_badge(task.state)
    put_off: date | None = task.defer_until if task.deferred(today) else None
    if task.is_project:
        where = standing(task, tasks or [], today)
        color, put_off = standing_color(where), where.until
        badge = Text.assemble(
            (" PROJECT ", f"bold #111111 on {PROJECT}"), " ", standing_badge(where)
        )
    rows: list[RenderableType] = [Text.assemble(badge, "  ", (task.title, "bold"))]
    if put_off:
        what = " (when its next task comes back)" if task.is_project else ""
        rows.append(
            Text.assemble(
                (f"Deferred until {put_off:%a} {put_off:%b} {put_off.day}", DEFERRED),
                (what, FAINT),
            )
        )
    if task.needs_title:
        rows.append(
            Text.assemble(
                ("Claude couldn't tell what this is. Name it: ", WARN),
                (f'todd edit {task.id} -t "…"', "bold"),
            )
        )
    if task.project:
        position = f"task {task.project_position} of " if task.project_position else ""
        rows.append(
            Text.assemble(
                ("▸ ", PROJECT),
                (position, FAINT),
                (f"#{task.project.id} {task.project.title}", PROJECT),
            )
        )
    if task.open_blockers:
        rows.append(
            Text(
                "Blocked by " + ", ".join(f"#{b.id} {b.title}" for b in task.open_blockers),
                style=PROJECT,
            )
        )
    if task.state == State.WAITING and (who := waiting_summary(task, names)):
        rows.append(Text(f"Waiting on reviews from {who}", style=WARN))
    elif task.state == State.WAITING and task.waiting_on:
        rows.append(Text(f"Waiting on {task.waiting_on}", style=WARN))
    if task.next_action and not task.state.closed:
        rows.append(Text.assemble(("→ ", ACCENT), task.next_action))
    rows += [followup_line(f, today) for f in task.open_followups]
    about = facts(task, today)
    if task.people:
        if about.plain:
            about.append(" · ", style=FAINT)
        about.append("with " + ", ".join(task.people))
    if about.plain:
        rows += [Text(), about]
    if not task.triaged and heading is None:
        rows += [Text(), Text("Not filed by Claude yet.", style=FAINT)]
    return Panel(
        Group(*rows),
        box=box.ROUNDED,
        border_style=color,
        padding=(0, 1),
        title=Text(f" {heading or f'#{task.id}'} ", style=f"bold {color}"),
        title_align="left",
        width=w,
    )


def quote_block(link: Link, names: Nicknames = NO_NAMES, width: int = 80) -> Text:
    """Quoted text with a bar down its left side, wrapped so the bar runs the whole way."""
    text = Text()
    lines = []
    for line in (link.quote or "").splitlines() or [""]:
        lines += textwrap.wrap(line, max(20, width - 2)) or [""]
    for i, line in enumerate(lines):
        if i:
            text.append("\n")
        text.append("│ ", style=SLACK if link.kind == LinkKind.SLACK else FAINT)
        text.append(line, style="italic")
    if link.author:
        text.append(f"\n  — {names.name(link.author)}", style=FAINT)
    return text


def link_block(
    link: Link,
    index: int,
    today: date,
    *,
    quotes: bool = True,
    names: Nicknames = NO_NAMES,
    stack_size: int | None = None,
    width: int = 100,
) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style=FAINT, no_wrap=True, width=3)
    grid.add_column()
    color = {LinkKind.JIRA: JIRA, LinkKind.SLACK: SLACK}.get(link.kind, "")
    head = Text()
    if stack_size and link.stack_position:
        head.append(f"{link.stack_position}/{stack_size} ", style=FAINT)
    head.append(linking.label(link), style=f"bold {color}".strip())
    if link.kind == LinkKind.SLACK and link.url and (message := linking.slack_message(link.url)):
        head.append(f" {message.posted.astimezone():%H:%M}", style=f"bold {color}")
    if link.status:
        head.append(f" · {link.status}")
    if link.role:
        head.append(f" · {link.role.label}", style=FAINT)
    lines: list[RenderableType] = [head]
    if link.title:
        title = Text(link.title)
        if link.kind == LinkKind.GITHUB and link.author:
            title.append(f" · by {names.name(link.author)}", style=FAINT)
        lines.append(title)
    if link.note:
        lines.append(Text(link.note, style=FAINT))
    if quotes and link.quote:
        lines.append(quote_block(link, names, width - 6))
    elif quotes and link.kind == LinkKind.SLACK:
        lines.append(Text("(no message text saved)", style=FAINT))
    if link.url:
        lines.append(Text(link.url, style=f"{FAINT} link {link.url}", overflow="fold"))
    grid.add_row(str(index), Group(*lines))
    return grid


def timeline(entries: Iterable[Entry], today: date) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=FAINT, no_wrap=True)
    grid.add_column()
    styles = {
        EntryKind.STATE: "bold",
        EntryKind.TRIAGE: FAINT,
        EntryKind.JIRA: JIRA,
        EntryKind.SLACK: SLACK,
    }
    for entry in entries:
        grid.add_row(when(entry.at, today), Text(entry.text, style=styles.get(entry.kind, "")))
    return grid


def stack_members(task: Task, key: str) -> list[tuple[int, Link]]:
    """A stack's pull requests on this task, bottom to top, with their link numbers."""
    members = [(i, link) for i, link in enumerate(task.links, 1) if link.stack == key]
    return sorted(members, key=lambda pair: pair[1].stack_position or 0)


def stack_header(key: str, members: list[Link]) -> Text:
    owner_repo, _, number = key.rpartition("/stacks/")
    states = [(link.status or "").split(" · ")[0] for link in members]
    tally = ", ".join(f"{states.count(s)} {s}" for s in dict.fromkeys(states) if s)
    return Text.assemble(
        ("    ", ""),
        (f"GitHub stack {number}", "bold"),
        (f" in {owner_repo} · {plural(len(members), 'pull request')}", FAINT),
        (f" · {tally}" if tally else "", FAINT),
        (" · bottom to top", FAINT),
    )


def task_mark(task: Task, today: date) -> Text:
    """One character for where a task stands: done, dropped, not yet startable, or open."""
    if task.state == State.DONE:
        return Text("✓", style=OK)
    if task.state == State.DROPPED:
        return Text("✗", style=FAINT)
    if task.blocked:
        return Text("◌", style=FAINT)
    if task.deferred(today):
        return Text("◌", style=DEFERRED)
    return Text("●", style=STATE_COLORS[task.state])


def progress(tasks: list[Task]) -> str:
    open_ = sum(1 for t in tasks if not t.state.closed)
    if not tasks:
        return "no tasks yet"
    return f"{open_} of {plural(len(tasks), 'task')} open"


def projects(
    console: Console,
    items: list[tuple[Task, list[Task]]],
    *,
    today: date,
    names: Nicknames = NO_NAMES,
) -> None:
    """Every project with its tasks."""
    w = width(console)
    console.print()
    if not items:
        console.print(Text("No projects.", style="bold"))
        console.print(
            Text.assemble(
                ("Describe the chain and Claude makes one: ", FAINT),
                (
                    'todd add "waiting on review for <PR>; then a bug bash; then …"',
                    f"bold {ACCENT}",
                ),
            )
        )
        console.print()
        return
    columns = Columns.fitting((t for _, tasks in items for t in tasks), today)
    for project, tasks in items:
        head = Text.assemble(
            ("▸ ", PROJECT), (f"#{project.id} ", FAINT), (project.title, f"bold {PROJECT}"), "  "
        )
        where = standing(project, tasks, today)
        head.append(standing_words(where, today), style=f"bold {standing_color(where)}")
        head.append(f" · {progress(tasks)}", style=FAINT)
        console.print(head, width=w)
        table = task_table(tasks, today=today, w=w - 2, names=names, states=True, columns=columns)
        console.print(Padding(table, (0, 0, 0, 2)), width=w)
        console.print()


def show(
    console: Console,
    task: Task,
    *,
    today: date,
    brief: bool = False,
    names: Nicknames = NO_NAMES,
    tasks: list[Task] | None = None,
) -> None:
    """One task, or a project (with `tasks`, its tasks)."""
    w = width(console)
    console.print()
    console.print(card(task, today, w, names, tasks=tasks))
    if task.is_project:
        console.print()
        console.print(section(f"Tasks · {progress(tasks or [])}", w, PROJECT), width=w)
        table = task_table(tasks or [], today=today, w=w - 2, names=names, states=True)
        console.print(Padding(table, (0, 0, 0, 2)), width=w)
    if task.description and task.description.strip() != task.title.strip():
        console.print()
        console.print(section("As you wrote it", w), width=w)
        console.print(Padding(Text(task.description), (0, 0, 0, 4)), width=w)
    if task.links:
        console.print()
        console.print(section("Links", w), width=w)
        shown: set[str] = set()
        for i, link in enumerate(task.links, 1):
            if link.stack is None:
                console.print(link_block(link, i, today, names=names, width=w), width=w)
                continue
            if link.stack in shown:
                continue
            shown.add(link.stack)
            members = stack_members(task, link.stack)
            console.print(stack_header(link.stack, [m for _, m in members]), width=w)
            for n, member in members:
                block = link_block(member, n, today, names=names, stack_size=len(members), width=w)
                console.print(block, width=w)
    if (board := review_table(task, names)) is not None:
        console.print()
        console.print(section("Reviews", w), width=w)
        console.print(Padding(board, (0, 0, 0, 4)), width=w)
        if who := waiting_summary(task, names, limit=99):
            console.print(Padding(Text(f"Waiting on {who}", style=WARN), (0, 0, 0, 4)), width=w)
    if task.followups:
        console.print()
        console.print(section("Follow-ups", w, FOLLOWUP), width=w)
        for followup in sorted(task.followups, key=lambda f: (not f.open, f.id or 0)):
            console.print(Padding(followup_line(followup, today), (0, 0, 0, 3)), width=w)
    if task.entries and not brief:
        console.print()
        console.print(section("Timeline", w), width=w)
        console.print(Padding(timeline(task.entries, today), (0, 0, 0, 4)), width=w)
    console.print()


# ── Previewing a filing ──────────────────────────────────────────────────────


def _as_task(filing: Any, state: State) -> Task:
    """A filing dressed as a task, so it can be shown the way it will look."""
    return Task(
        title=filing.title,
        state=state,
        next_action=filing.next_action,
        area=filing.area,
        priority=filing.priority,
        due=filing.due,
        due_hint=filing.due_hint,
        defer_until=filing.defer,
        waiting_on=filing.waiting_on,
        needs_title=filing.needs_title,
        people=filing.people,
        followups=filing.followups,
        is_project=filing.is_project,
        triaged_at=datetime.now(),
    )


def preview(
    console: Console,
    filing: Any,
    links: list[Link],
    *,
    today: date,
    state: State | None = None,
    owners: dict[int, int] | None = None,
    names: Nicknames = NO_NAMES,
) -> None:
    """What Claude would file, before anything is saved: the card, what each link is for and,
    for a project, its tasks with their links and what each waits on."""
    w = width(console)
    shown = State.TODO if filing.is_project else (state or filing.track)
    proposed = [
        Task(
            item.title,
            state=item.track,
            blockers=[TaskRef(-n, "", State.TODO) for n in item.after],
            defer_until=item.defer,
        )
        for item in filing.tasks
    ]
    console.print()
    console.print(
        card(
            _as_task(filing, shown),
            today,
            w,
            names,
            heading="Claude would file this · not saved yet",
            tasks=proposed,
        )
    )
    owners = owners or {}
    if links:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="right", style=FAINT, no_wrap=True, width=3)
        grid.add_column()
        for index, link in enumerate(links, 1):
            verdict = filing.links.get(index)
            line = Text(linking.label(link), style="bold")
            role = link.role if link.role_fixed else (verdict.role if verdict else None)
            if role:
                line.append(f" · {role.label}", style=FAINT)
            if verdict and verdict.note:
                line.append(f" · {verdict.note}", style=FAINT)
            if index in owners:
                line.append(f"  → task {owners[index]}", style=PROJECT)
            grid.add_row(str(index), line)
        console.print(section("Links", w), width=w)
        console.print(grid, width=w)
    if filing.is_project:
        table = Table(
            box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True
        )
        table.add_column(justify="right", style=FAINT, no_wrap=True, width=3)
        table.add_column(no_wrap=True, width=1)
        table.add_column(ratio=1)
        table.add_column(max_width=LINKS_WIDTH)
        for number, item in enumerate(filing.tasks, 1):
            detail = None
            if item.after:
                detail = Text("after " + ", ".join(f"task {n}" for n in item.after), style=FAINT)
            elif item.defer and item.defer > today:
                detail = Text(f"deferred {until_text(item.defer, today)}", style=DEFERRED)
            elif item.track == State.WAITING and item.waiting_on:
                detail = Text(f"waiting on {item.waiting_on}", style=WARN)
            elif item.next_action:
                detail = Text(f"→ {item.next_action}", style=FAINT)
            title = Text(item.title)
            if item.needs_title:
                title.append("  (needs a title)", style=WARN)
            mine = [linking.label(links[i - 1]) for i, n in sorted(owners.items()) if n == number]
            table.add_row(
                str(number),
                Text("◌" if item.after else "●", style=FAINT if item.after else OK),
                Group(title, detail) if detail is not None else title,
                Text(" · ".join(mine), style=FAINT),
            )
        console.print(section(f"Tasks · {plural(len(filing.tasks), 'task')}", w, PROJECT), width=w)
        console.print(table, width=w)
    console.print()


def update_preview(
    console: Console,
    task: Task,
    update: Any,
    diff: dict[str, tuple[str | None, str | None]],
) -> None:
    """What a pull would change on a task, field by field, before it changes."""
    w = width(console)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=FAINT, no_wrap=True)
    grid.add_column()
    for name, (now, proposed) in diff.items():
        grid.add_row(
            name,
            Text.assemble(
                (now or "nothing", FAINT), (" → ", FAINT), (proposed or "nothing", "bold")
            ),
        )
    if update.reason:
        grid.add_row("why", Text(update.reason, style="italic"))
    console.print()
    console.print(
        Panel(
            Group(Text(task.title, style="bold"), Text(), grid),
            box=box.ROUNDED,
            border_style=ACCENT,
            padding=(0, 1),
            title=Text(f" Pull would update #{task.id} · not saved yet ", style=f"bold {ACCENT}"),
            title_align="left",
            width=w,
        )
    )


def plan(
    console: Console, steps: list[Any], *, then: str | None = None, more: bool = False
) -> None:
    """What todd is about to do for a request: each step, and the command it runs. `then` is
    what Claude will work out once these have run; `more` marks a later round of the same
    request."""
    w = width(console)
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    table.add_column(justify="right", style=FAINT, no_wrap=True, width=3)
    table.add_column(ratio=1)
    for i, step in enumerate(steps, 1):
        table.add_row(
            str(i),
            Group(Text(step.says, style="bold"), Text(step.command_line, style=FAINT)),
        )
    if then:
        table.add_row("…", Text.assemble(("then: ", FAINT), (then, "italic")))
    console.print()
    console.print(section("Next, todd will" if more else "todd will", w, ACCENT), width=w)
    console.print(table, width=w)
    console.print()


# ── Numbers ──────────────────────────────────────────────────────────────────


def _moves(changes: dict[int, int], mark: str) -> list[str]:
    """Number changes in a few words, runs together: "#4 to #6 are now #3 to #5"."""
    runs: list[list[tuple[int, int]]] = []
    for old, new in sorted(changes.items()):
        if runs and runs[-1][-1] == (old - 1, new - 1):
            runs[-1].append((old, new))
        else:
            runs.append([(old, new)])
    said = []
    for run in runs:
        (first, to_first), (last, to_last) = run[0], run[-1]
        if len(run) == 1:
            said.append(f"{mark}{first} is now {mark}{to_first}")
        else:
            said.append(
                f"{mark}{first} to {mark}{last} are now {mark}{to_first} to {mark}{to_last}"
            )
    return said


def renumbered(tasks: dict[int, int], followups: dict[int, int]) -> Text | None:
    """What moved when numbers were brought back down, or None if nothing worth saying did."""
    parts = [*_moves(tasks, "#"), *_moves(followups, "↪")]
    if not parts:
        return None
    return Text("  Renumbered: " + " · ".join(parts), style=FAINT)


# ── Capturing ────────────────────────────────────────────────────────────────


def capture_line(capture: Capture, origin: str, w: int) -> Text:
    words = len([word for word in capture.description.split() if not linking.recognize(word)])
    line = Text()
    line.append("◇ ", style=ACCENT)
    line.append(origin, style=f"bold {ACCENT}")
    meta = [plural(words, "word")] if words else []
    if capture.links:
        meta.append(plural(len(capture.links), "link"))
    if meta:
        line.append(" · " + " · ".join(meta), style=FAINT)
    if capture.description:
        line.append(" · ", style=FAINT)
        line.append(f"“{' '.join(capture.description.split())}”", style=f"italic {FAINT}")
    return line


def lookup_line(
    link: Link,
    problem: str | None,
    w: int,
    *,
    names: Nicknames = NO_NAMES,
    new: bool = False,
    via: str | None = None,
) -> Text:
    """One line per link after todd has tried to look it up. `via` is the pull request whose
    title named this ticket."""
    color = {LinkKind.JIRA: JIRA, LinkKind.SLACK: SLACK}.get(link.kind, "")
    line = Text("  ")
    if problem:
        line.append("✗ ", style=ERROR)
    elif new:
        line.append("+ ", style=OK)
    elif link.kind in (LinkKind.JIRA, LinkKind.GITHUB) and link.fetched_at:
        line.append("✓ ", style=OK)
    else:
        line.append("· ", style=FAINT)
    line.append(linking.label(link), style=color)
    if problem:
        line.append(f" · {problem}", style=FAINT)
    elif link.kind == LinkKind.SLACK:
        if link.quote:
            n = len(link.quote.split())
            line.append(f" · message text kept ({plural(n, 'word')})", style=FAINT)
        else:
            line.append(" · no message text", style=FAINT)
    else:
        if link.stack_position:
            line.append(f" ({link.stack_position} in its stack)", style=FAINT)
        if link.title:
            line.append(f" · {link.title}")
        if link.status:
            line.append(f" · {link.status}", style=FAINT)
        if link.kind == LinkKind.GITHUB and link.author:
            line.append(f" · {names.name(link.author)}", style=FAINT)
    if via:
        line.append(f" · from the title of #{via.rsplit('#', 1)[-1]}", style=FAINT)
    return line
