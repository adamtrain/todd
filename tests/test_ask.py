"""The arrow-key chooser, including in a real pseudo-terminal."""

import io
import os
import pty
import select
import signal
import sys
import termios
import threading

import pytest
from rich.console import Console

from todd import ask

OPTIONS = [
    ask.Option("draft", "Draft a message"),
    ask.Option("told", "Told them"),
    ask.Option("later", "Later"),
]


@pytest.mark.parametrize(
    ("start", "key", "expected"),
    [
        (0, "right", (1, False)),
        (2, "right", (0, False)),  # wraps around
        (0, "left", (2, False)),
        (1, "tab", (2, False)),
        (1, "shift-tab", (0, False)),
        (0, "down", (1, False)),
        (0, "l", (2, False)),  # a letter jumps, but doesn't pick
        (0, "T", (1, False)),
        (0, "x", (0, False)),
        (1, "enter", (1, True)),
    ],
)
def test_keys(start, key, expected):
    assert ask.step(start, key, OPTIONS) == expected


def test_a_stray_y_does_not_say_yes():
    assert ask.step(0, "y", ask.YES_NO) == (1, False)


def test_reading_keys_from_the_terminal():
    read, write = os.pipe()
    try:
        for data, key in [
            (b"\x1b[C", "right"),
            (b"\x1b[D", "left"),
            (b"\x1bOA", "up"),
            (b"\x1b[Z", "shift-tab"),
            (b"\r", "enter"),
            (b"\n", "enter"),
            (b"\t", "tab"),
            (b"y", "y"),
            (b"\x1b", "escape"),  # Esc on its own
        ]:
            os.write(write, data)
            assert ask.read_key(read) == key
    finally:
        os.close(read)
        os.close(write)


def test_how_it_looks():
    choosing = ask.line("Move PLAT-412 to Done?", ask.YES_NO, 0)
    assert choosing.plain == "  Move PLAT-412 to Done?   No   Yes    ←/→ Enter"
    highlighted = [s for s in choosing.spans if "on " in str(s.style)]
    assert [choosing.plain[s.start : s.end] for s in highlighted] == [" No "]
    assert ask.line("Move PLAT-412 to Done?", ask.YES_NO, 1, done=True).plain == (
        "  Move PLAT-412 to Done?  Yes"
    )


@pytest.fixture
def terminal(monkeypatch):
    """A real pseudo-terminal as stdin. Returns a function that types into it, a moment after
    the chooser has started (it throws away anything typed before then)."""
    main, replica = pty.openpty()
    stdin = os.fdopen(replica, "r")
    monkeypatch.setattr(sys, "stdin", stdin)
    before = termios.tcgetattr(replica)
    signal.signal(signal.SIGALRM, lambda *_: pytest.fail("the chooser hung"))
    signal.alarm(5)

    def type_soon(keys: bytes, *, ahead: bytes = b"") -> None:
        if ahead:
            os.write(main, ahead)
            # A real terminal reads back the echo of what was typed; do the same, or the
            # terminal never drains and changing its mode waits forever.
            while select.select([main], [], [], 0.1)[0]:
                os.read(main, 1024)
        threading.Timer(0.3, os.write, (main, keys)).start()

    try:
        yield type_soon
        after = termios.tcgetattr(replica)
        # The kernel may set PENDIN ("retype pending input") itself; everything else must match.
        pending = getattr(termios, "PENDIN", 0)
        after[3] &= ~pending
        before[3] &= ~pending
        assert after == before, "the terminal wasn't put back the way it was"
    finally:
        signal.alarm(0)
        stdin.close()
        os.close(main)


def pick(typer, keys: bytes, *, default: int = 0, ahead: bytes = b"") -> tuple[str, str]:
    console = Console(file=io.StringIO(), force_terminal=True, width=80, color_system=None)
    typer(keys, ahead=ahead)
    picked = ask.choose("Move PLAT-412 to Done?", ask.YES_NO, default=default, console=console)
    return picked, console.file.getvalue()  # ty: ignore[unresolved-attribute]


def test_enter_takes_the_default(terminal):
    picked, shown = pick(terminal, b"\r")
    assert picked == "no"
    assert "\x1b[?25l" in shown and "\x1b[?25h" in shown  # cursor hidden, then shown again
    assert shown.endswith("\r\x1b[2K  Move PLAT-412 to Done?  No\n")


def test_arrow_then_enter(terminal):
    assert pick(terminal, b"\x1b[C\r")[0] == "yes"


def test_letter_then_enter(terminal):
    assert pick(terminal, b"y\r")[0] == "yes"


def test_escape_takes_the_default(terminal):
    assert pick(terminal, b"\x1b[C\x1b")[0] == "no"


def test_typing_ahead_cannot_answer(terminal):
    # An Enter pressed before the question appeared is thrown away, not taken as "No".
    assert pick(terminal, b"\x1b[C\r", ahead=b"\r")[0] == "yes"


def test_without_a_terminal_it_asks_for_a_letter(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    monkeypatch.setattr(ask.os, "isatty", lambda fd: False)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0, raising=False)
    console = Console(file=io.StringIO(), width=80)
    assert ask.choose("Move it?", ask.YES_NO, default=0, console=console) == "yes"
