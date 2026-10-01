"""The desktop: opening links, the clipboard, and your editor."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from todd import proc
from todd.errors import ToddError


def _utf8() -> dict[str, str]:
    # pbcopy and pbpaste transcode to the locale's charset, so make sure that's UTF-8.
    return {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}


def open_url(url: str) -> None:
    command = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        result = proc.run([command, url], timeout=15)
    except proc.ProcError as e:
        raise ToddError(f"Couldn't open {url}.", detail=str(e)) from e
    if not result.ok:
        raise ToddError(f"Couldn't open {url}.", detail=result.complaint)


def copy(text: str) -> None:
    command = ["pbcopy"] if sys.platform == "darwin" else ["xclip", "-selection", "clipboard"]
    try:
        subprocess.run(command, input=text.encode(), env=_utf8(), timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise ToddError("Couldn't copy to the clipboard.", detail=str(e)) from e


def _editor() -> str:
    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"


def edit_file(path: Path) -> None:
    editor = _editor()
    try:
        subprocess.run([*shlex.split(editor), str(path)], check=False)
    except OSError as e:
        raise ToddError(
            f"Couldn't start your editor ({editor}).",
            hint="Set [bold]$EDITOR[/], e.g. [bold]export EDITOR=nano[/].",
        ) from e


def edit(text: str, *, suffix: str = ".md") -> str:
    """Let the person edit `text` in their editor; return what they saved."""
    editor = _editor()
    with tempfile.TemporaryDirectory(prefix="todd-") as scratch:
        path = Path(scratch) / f"capture{suffix}"
        path.write_text(text, encoding="utf-8")
        try:
            done = subprocess.run([*shlex.split(editor), str(path)], check=False)
        except OSError as e:
            raise ToddError(
                f"Couldn't start your editor ({editor}).",
                hint="Set [bold]$EDITOR[/], e.g. [bold]export EDITOR=nano[/].",
            ) from e
        if done.returncode != 0:
            raise ToddError(f"{editor} exited with an error, so nothing was captured.")
        return path.read_text(encoding="utf-8")
