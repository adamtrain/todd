"""Nicknames: the names you use for people, keyed by their GitHub login.

Kept in nicknames.toml beside the config (todd rewrites it, so it has no comments):

    "priya-n" = "Priya"
    "acme/platform-reviewers" = "Platform reviewers"
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from todd.errors import ToddError


def _login(login: str) -> str:
    return login.strip().lstrip("@").lower()


class Nicknames:
    def __init__(self, path: Path | None = None, names: dict[str, str] | None = None):
        self.path = path
        self._names: dict[str, str] = {}
        if names is not None:
            self._names = {_login(k): v.strip() for k, v in names.items() if v.strip()}
        elif path is not None and path.exists():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError) as e:
                raise ToddError(f"Couldn't read nicknames from {path}.", detail=str(e)) from e
            self._names = {
                _login(k): str(v).strip()
                for k, v in data.items()
                if isinstance(v, str) and v.strip()
            }

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._names.items())

    def get(self, login: str) -> str | None:
        return self._names.get(_login(login))

    def name(self, login: str | None) -> str | None:
        """How to show a GitHub login: its nickname if it has one."""
        if not login:
            return None
        return self.get(login) or login

    def describe(self, login: str | None) -> str | None:
        """For Claude: the name and the login, so it can connect the two."""
        if not login:
            return None
        nickname = self.get(login)
        return f"{nickname} (GitHub @{login})" if nickname else f"GitHub @{login}"

    def aliases(self, who: str) -> list[str]:
        """Every way this person might be recorded: the text itself, plus login ↔ nickname."""
        found = [who.strip().lstrip("@")]
        if nickname := self.get(who):
            found.append(nickname)
        found += [login for login, name in self._names.items() if name.lower() == who.lower()]
        return list(dict.fromkeys(found))

    def set(self, login: str, name: str | None) -> None:
        key = _login(login)
        if not key:
            raise ToddError("A GitHub login can't be empty.")
        if name and name.strip():
            self._names[key] = name.strip()
        else:
            self._names.pop(key, None)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        lines = [f"{json.dumps(k)} = {json.dumps(v, ensure_ascii=False)}" for k, v in self.items()]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError as e:
            raise ToddError(f"Couldn't save nicknames to {self.path}.", detail=str(e)) from e
