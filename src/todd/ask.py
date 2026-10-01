"""Picking one of a few options with the arrow keys and Enter, right in the terminal.

    Move PLAT-412 to Done?   No   Yes     ←/→ Enter

The highlight starts on the default. ←/→ (or ↑/↓, Tab) move it; typing an option's first
letter jumps to it; Enter picks; Esc picks the default. Nothing is picked by a single stray key.
"""

from __future__ import annotations

import os
import select
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

ACCENT = "#7c83f7"
FAINT = "grey42"

_SEQUENCES = {
    b"[A": "up",
    b"[B": "down",
    b"[C": "right",
    b"[D": "left",
    b"OA": "up",
    b"OB": "down",
    b"OC": "right",
    b"OD": "left",
    b"[Z": "shift-tab",
}


@dataclass(frozen=True, slots=True)
class Option:
    key: str  # what choose() returns
    label: str  # what's shown; its first letter jumps to it


YES_NO = [Option("no", "No"), Option("yes", "Yes")]


def step(index: int, key: str, options: Sequence[Option]) -> tuple[int, bool]:
    """Apply one keypress. Returns the highlighted option and whether it was picked."""
    if key == "enter":
        return index, True
    if key in ("left", "up", "shift-tab"):
        return (index - 1) % len(options), False
    if key in ("right", "down", "tab"):
        return (index + 1) % len(options), False
    if len(key) == 1:
        for i, option in enumerate(options):
            if option.label[:1].lower() == key.lower():
                return i, False
    return index, False


def read_key(fd: int) -> str:
    """One keypress from a terminal in cbreak mode, named: enter, left, escape, "y"…"""
    ch = os.read(fd, 1)
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b"\t":
        return "tab"
    if ch == b"\x04":
        return "escape"
    if ch == b"\x1b":
        sequence = b""
        while len(sequence) < 2 and select.select([fd], [], [], 0.05)[0]:
            sequence += os.read(fd, 1)
        return _SEQUENCES.get(sequence, "escape")
    return ch.decode("utf-8", errors="ignore")


def line(question: str | Text, options: Sequence[Option], index: int, *, done: bool = False):
    """The prompt as it looks while choosing, or once chosen."""
    text = Text("  ")
    text.append_text(question if isinstance(question, Text) else Text(question, style="bold"))
    text.append("  ")
    if done:
        return text.append(options[index].label, style=f"bold {ACCENT}")
    for i, option in enumerate(options):
        if i:
            text.append(" ")
        if i == index:
            text.append(f" {option.label} ", style=f"bold #111111 on {ACCENT}")
        else:
            text.append(f" {option.label} ", style=FAINT)
    return text.append("   ←/→ Enter", style=FAINT)


def _draw(console: Console, text: Text) -> None:
    text.truncate(max(10, console.width - 1), overflow="ellipsis")
    console.file.write("\r\x1b[2K")
    console.print(text, end="", soft_wrap=True)
    console.file.flush()


def choose(
    question: str | Text,
    options: Sequence[Option],
    *,
    default: int = 0,
    console: Console,
    read: Callable[[int], str] = read_key,
) -> str:
    """Let the person pick one option; returns its key."""
    fd = sys.stdin.fileno()
    try:
        import termios
        import tty
    except ImportError:  # no termios (Windows): fall back to typing a letter
        return _typed(question, options, default, console)
    if not os.isatty(fd):
        return _typed(question, options, default, console)
    saved = termios.tcgetattr(fd)
    index = default
    try:
        # TCSAFLUSH throws away anything typed before the question appeared, so an Enter
        # pressed early can't answer a question you haven't seen yet.
        tty.setcbreak(fd, termios.TCSAFLUSH)
        console.file.write("\x1b[?25l")  # hide the cursor while choosing
        while True:
            _draw(console, line(question, options, index))
            key = read(fd)
            if key == "escape":
                index = default
                break
            index, picked = step(index, key, options)
            if picked:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        console.file.write("\x1b[?25h")
        console.file.flush()
    _draw(console, line(question, options, index, done=True))
    console.file.write("\n")
    return options[index].key


def _typed(question: str | Text, options: Sequence[Option], default: int, console: Console):
    letters = [option.label[:1].lower() for option in options]
    labels = " · ".join(f"[bold]{o.label[:1]}[/]{o.label[1:]}" for o in options)
    prompt = Text.assemble("  ", question, " ", Text.from_markup(labels))
    picked = Prompt.ask(
        prompt, console=console, choices=letters, default=letters[default], show_choices=False
    )
    return options[letters.index(picked)].key
