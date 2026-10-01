"""Running other programs (claude, acli, gh, open, pbcopy). Tests replace `run`."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProcError(Exception):
    def __init__(self, message: str, *, missing: bool = False, timed_out: bool = False):
        super().__init__(message)
        self.missing = missing
        self.timed_out = timed_out


@dataclass(frozen=True, slots=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def complaint(self) -> str:
        """The most useful line or two of whatever the program said when it failed."""
        text = (self.stderr.strip() or self.stdout.strip()).splitlines()
        return "\n".join(text[-3:])


def run(
    argv: list[str],
    *,
    input: str | None = None,
    timeout: float | None = 60,
    cwd: Path | None = None,
) -> Result:
    """Run a program to completion, never leaving it waiting on the terminal for input."""
    if shutil.which(argv[0]) is None:
        raise ProcError(f"{argv[0]} isn't installed or isn't on your PATH.", missing=True)
    try:
        done = subprocess.run(
            argv,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ProcError(f"{argv[0]} took longer than {timeout:g}s.", timed_out=True) from e
    except OSError as e:
        raise ProcError(f"Couldn't run {argv[0]}: {e.strerror or e}") from e
    return Result(done.returncode, done.stdout or "", done.stderr or "")
