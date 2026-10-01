"""Asking Claude things through Claude Code's headless mode (`claude -p`).

That way todd runs on whatever Claude account Claude Code is signed in to, with no API key.
Each call is a single turn with every tool switched off: todd gathers the context itself
and Claude only reads it and answers.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from todd import proc
from todd.config import ClaudeConfig
from todd.errors import ToddError


def result_of(reply: Any) -> dict[str, Any] | None:
    """The result object from `claude -p --output-format json`.

    Usually that's the whole reply. In verbose mode (`--verbose`, or `"verbose": true` in
    Claude Code's settings, which an employer can set) it's a list of every message in the
    conversation instead, with the result last.
    """
    if isinstance(reply, dict):
        return reply
    if isinstance(reply, list):
        results = [m for m in reply if isinstance(m, dict) and m.get("type") == "result"]
        return results[-1] if results else None
    return None


class Claude:
    def __init__(self, config: ClaudeConfig | None = None):
        self.config = config or ClaudeConfig()

    def argv(self, system: str, schema: dict[str, Any] | None = None) -> list[str]:
        c = self.config
        argv = [
            c.command,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--system-prompt",
            system,
        ]
        if c.model:
            argv += ["--model", c.model]
        if c.effort:
            argv += ["--effort", c.effort]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        return argv + list(c.args)

    def _call(self, prompt: str, system: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        # Run somewhere neutral so a project's CLAUDE.md doesn't leak into the prompt.
        with tempfile.TemporaryDirectory(prefix="todd-") as scratch:
            try:
                result = proc.run(
                    self.argv(system, schema),
                    input=prompt,
                    timeout=self.config.timeout,
                    cwd=Path(scratch),
                )
            except proc.ProcError as e:
                hint = None
                if e.missing:
                    hint = (
                        "Install Claude Code and sign in, or set [bold]command[/] under "
                        "[bold][claude][/] in your todd config."
                    )
                elif e.timed_out:
                    hint = "Raise [bold]timeout[/] under [bold][claude][/] in your todd config."
                raise ToddError(str(e), hint=hint) from e
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ToddError(
                "Claude Code didn't answer."
                if not result.ok
                else "Claude Code's reply wasn't JSON.",
                detail=result.complaint or None,
                hint='Check that [bold]claude -p "hi"[/] works in your terminal.',
            ) from None
        envelope = result_of(envelope)
        if envelope is None:
            raise ToddError(
                "Claude Code's reply wasn't what todd expected.",
                detail=f"It sent: {result.stdout.strip()[:300]}",
                hint="Please send this along with [bold]claude --version[/].",
            )
        if envelope.get("is_error") or not result.ok:
            detail = envelope.get("result") or envelope.get("subtype") or result.complaint
            raise ToddError("Claude couldn't finish.", detail=str(detail)[:500])
        return envelope

    def structured(self, prompt: str, *, system: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One answer shaped by `schema`."""
        envelope = self._call(prompt, system, schema)
        answer = envelope.get("structured_output")
        if answer is None and isinstance(envelope.get("result"), str):
            # Older Claude Code versions only return the text; it should still be the JSON.
            try:
                answer = json.loads(envelope["result"])
            except json.JSONDecodeError:
                answer = None
        if not isinstance(answer, dict):
            raise ToddError(
                "Claude didn't return a structured answer.",
                detail=str(envelope.get("result"))[:300],
                hint="Your Claude Code may be too old for [bold]--json-schema[/]: "
                "try [bold]claude update[/].",
            )
        return answer

    def text(self, prompt: str, *, system: str) -> str:
        envelope = self._call(prompt, system, None)
        answer = envelope.get("result")
        if not isinstance(answer, str) or not answer.strip():
            raise ToddError("Claude came back empty-handed.")
        return answer.strip()
