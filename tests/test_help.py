"""`todd --help` lists every command, grouped and in a sensible order."""

import re

from typer.testing import CliRunner

from todd import cli


def visible_commands() -> set[str]:
    commands = {cli.command_name(info) for info in cli.app.registered_commands if not info.hidden}
    groups = {group.name for group in cli.app.registered_groups if not group.hidden}
    return commands | {name for name in groups if name}


def test_every_command_has_a_place_in_the_help():
    laid_out = [name for names in cli.HELP_LAYOUT.values() for name in names]
    assert len(laid_out) == len(set(laid_out)), "a command is listed twice"
    assert set(laid_out) == visible_commands()


def test_help_follows_the_layout(monkeypatch):
    monkeypatch.setenv("COLUMNS", "120")
    colored = CliRunner().invoke(cli.app, ["--help"]).output
    # On GitHub Actions the help comes out in color even when captured.
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", colored)
    headings = [line for line in help_text.splitlines() if line.startswith("╭─")]
    assert [re.sub(r"[╭─╮ ]+", " ", h).strip() for h in headings] == [
        "Options",
        *cli.HELP_LAYOUT,
    ]
    listed = re.findall(r"^│ ([a-z]+) ", help_text, flags=re.MULTILINE)
    assert listed == [name for names in cli.HELP_LAYOUT.values() for name in names]
