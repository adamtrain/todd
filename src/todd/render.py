"""Everything todd prints: the queue, task cards, capture progress, and errors."""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
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
    State,
    Task,
)
from todd.people import Nicknames

NO_NAMES = Nicknames()

MAX_WIDTH = 120

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

# The order sections appear in the queue: what you're on, then what's next, then what's stuck.
QUEUE_ORDER = [State.DOING, State.IN_REVIEW, State.TODO, State.WAITING, State.INBOX]
CLOSED_ORDER = [State.DONE, State.DROPPED]

PRIORITY_MARKS = {
    Priority.URGENT: ("‼", f"bold {ERROR}"),
    Priority.HIGH: ("!", f"bold {WARN}"),
    Priority.NORMAL: (" ", ""),
    Priority.LOW: ("↓", FAINT),
}


def width(console: Console) -> int:
    return min(console.width, MAX_WIDTH)


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


def state_word(state: State) -> Text:
    return Text(state.label, style=f"bold {STATE_COLORS[state]}")


def link_badges(task: Task) -> Text:
    """A compact summary of a task's links for the queue."""
    text = Text(no_wrap=True, overflow="ellipsis")
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
    parts += [Text(f"stack of {n}") for n in stacks.values()]
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


# ── The queue ────────────────────────────────────────────────────────────────


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


def _second_line(task: Task, names: Nicknames) -> Text | None:
    if task.state == State.INBOX:
        return Text.assemble(
            ("not filed yet · ", FAINT), (f"todd triage {task.id}", f"{FAINT} bold")
        )
    if task.needs_title:
        return Text.assemble(
            ("needs a title · ", WARN), (f'todd edit {task.id} -t "…"', f"{FAINT} bold")
        )
    if task.state == State.WAITING and (who := waiting_summary(task, names)):
        return Text(f"on reviews from {who}", style=WARN)
    if task.state == State.WAITING and task.waiting_on:
        return Text(f"on {task.waiting_on}", style=WARN)
    if task.next_action and not task.state.closed:
        # The next step is a reminder, so one line of it will do; who you're waiting on wraps.
        return Text(f"→ {task.next_action}", style=FAINT, no_wrap=True, overflow="ellipsis")
    return None


def queue_columns(w: int) -> list[str]:
    """Which queue columns fit: the task itself always gets at least ~36 characters."""
    columns = ["id", "mark", "task", "kind", "project", "due", "links", "age"]
    if w < 120:
        columns.remove("kind")
    if w < 100:
        columns.remove("project")
    if w < 70:
        columns.remove("links")
    return columns


def _queue_table(
    tasks: list[Task], today: date, now: datetime, w: int, names: Nicknames = NO_NAMES
) -> Table:
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    columns = queue_columns(w)
    specs: dict[str, dict[str, Any]] = {
        "id": {"justify": "right", "style": FAINT, "no_wrap": True, "width": 5},
        "mark": {"no_wrap": True, "width": 1},
        "task": {"ratio": 1},
        "kind": {"no_wrap": True, "width": 10, "style": FAINT},
        "project": {"no_wrap": True, "width": 12, "overflow": "ellipsis"},
        "due": {"no_wrap": True, "width": 11},
        "links": {"no_wrap": True, "width": 18 if w >= 100 else 16, "overflow": "ellipsis"},
        "age": {"no_wrap": True, "width": 4, "justify": "right", "style": FAINT},
    }
    for name in columns:
        table.add_column(**specs[name])
    for task in tasks:
        mark, mark_style = PRIORITY_MARKS[task.priority]
        closed = task.state.closed
        title = Text(task.title, style="strike " + FAINT if task.state == State.DROPPED else "")
        if closed and task.state != State.DROPPED:
            title.stylize(FAINT)
        body: RenderableType = title
        if (second := _second_line(task, names)) is not None:
            body = Group(title, second)
        cells: dict[str, RenderableType] = {
            "id": f"#{task.id}",
            "mark": Text(mark, style=mark_style),
            "task": body,
            "kind": task.kind.label if task.kind else "",
            "project": Text(task.project or "", style=FAINT if closed else ""),
            "due": due_text(task.due if not closed else None, today),
            "links": link_badges(task),
            "age": ago(task.state_at, now),
        }
        table.add_row(*(cells[name] for name in columns))
    return table


def _due_followups(items: list[tuple[Followup, Task]], today: date, w: int) -> Table:
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1), width=w, expand=True)
    table.add_column(justify="right", style=FOLLOWUP, no_wrap=True, width=5)
    table.add_column(ratio=1)
    table.add_column(no_wrap=True, width=11)
    for followup, task in items:
        about = Text(f"#{task.id} {task.title}", style=FAINT, no_wrap=True, overflow="ellipsis")
        table.add_row(
            f"↪{followup.id}", Group(Text(followup.action), about), trigger_text(followup, today)
        )
    return table


def queue(
    console: Console,
    tasks: list[Task],
    *,
    today: date,
    now: datetime,
    done_this_week: int = 0,
    closed: bool = False,
    due: list[tuple[Followup, Task]] | None = None,
    following: int = 0,
    names: Nicknames = NO_NAMES,
) -> None:
    w = width(console)
    order = QUEUE_ORDER + (CLOSED_ORDER if closed else [])
    groups = {
        state: sorted((t for t in tasks if t.state == state), key=_sort_key) for state in order
    }
    if due:
        console.print()
        console.print(
            Text.assemble(
                ("↪ ", FOLLOWUP), ("Follow-ups due", f"bold {FOLLOWUP}"), (f" {len(due)}", FAINT)
            )
        )
        console.print(_due_followups(due, today, w))
    if not any(groups.values()) and not due:
        console.print()
        console.print(Text("Nothing on your list.", style="bold"))
        console.print(
            Text.assemble(
                ("Add something: ", FAINT),
                ('todd add "Reply to Priya about the migration" ', f"bold {ACCENT}"),
                ("https://…slack.com/archives/…", ACCENT),
            )
        )
        if following:
            console.print(
                Text.assemble(
                    (f"{following} following", STATE_COLORS[State.FOLLOWING]),
                    (" (todd following)", FAINT),
                )
            )
        console.print()
        return
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
        console.print(_queue_table(group, today, now, w, names))
        console.print()
    tally = [
        Text.assemble((str(len(groups[s])), f"bold {STATE_COLORS[s]}"), (f" {s.label}", FAINT))
        for s in QUEUE_ORDER
        if groups[s]
    ]
    footer = Text()
    for i, part in enumerate(tally):
        if i:
            footer.append(" · ", style=FAINT)
        footer.append_text(part)
    if following:
        footer.append("   ")
        footer.append(f"{following} following", style=STATE_COLORS[State.FOLLOWING])
        footer.append(" (todd following)", style=FAINT)
    if done_this_week:
        footer.append(f"   ✓ {done_this_week} done this week", style=STATE_COLORS[State.DONE])
    if footer.plain:
        console.print(footer)
    console.print()


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
    table.add_column(justify="right", style=FAINT, no_wrap=True, width=5)
    table.add_column(ratio=1)
    table.add_column(no_wrap=True, width=16, overflow="ellipsis")
    table.add_column(no_wrap=True, width=4, justify="right", style=FAINT)
    for task in tasks:
        body: list[RenderableType] = [Text(task.title)]
        if (check := next_check(task)) is not None:
            line = Text.assemble(("↪ ", FOLLOWUP), check.action, "  ")
            line.append_text(trigger_text(check, today))
            line.no_wrap, line.overflow = True, "ellipsis"
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
    """kind · project · priority · due, whichever are known."""
    parts: list[Text] = []
    if task.kind:
        parts.append(Text(task.kind.label))
    if task.project:
        parts.append(Text(task.project, style=ACCENT))
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


def card(task: Task, today: date, w: int, names: Nicknames = NO_NAMES) -> Panel:
    color = STATE_COLORS[task.state]
    rows: list[RenderableType] = [
        Text.assemble(state_badge(task.state), "  ", (task.title, "bold"))
    ]
    if task.needs_title:
        rows.append(
            Text.assemble(
                ("Claude couldn't tell what this is. Name it: ", WARN),
                (f'todd edit {task.id} -t "…"', "bold"),
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
    if not task.triaged:
        rows += [Text(), Text("Not filed by Claude yet.", style=FAINT)]
    return Panel(
        Group(*rows),
        box=box.ROUNDED,
        border_style=color,
        padding=(0, 1),
        title=Text(f" #{task.id} ", style=f"bold {color}"),
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
    width: int = MAX_WIDTH,
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


def show(
    console: Console,
    task: Task,
    *,
    today: date,
    brief: bool = False,
    names: Nicknames = NO_NAMES,
) -> None:
    w = width(console)
    console.print()
    console.print(card(task, today, w, names))
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
    line.truncate(w, overflow="ellipsis")
    return line


def lookup_line(
    link: Link, problem: str | None, w: int, *, names: Nicknames = NO_NAMES, new: bool = False
) -> Text:
    """One line per link after todd has tried to look it up."""
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
    line.truncate(w, overflow="ellipsis")
    return line
