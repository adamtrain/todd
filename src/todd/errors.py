"""One error type that knows how to explain itself."""

from __future__ import annotations


class ToddError(Exception):
    """Anything that went wrong, phrased for a person. `hint` may use Rich markup."""

    def __init__(self, message: str, *, hint: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.hint = hint
        self.detail = detail
