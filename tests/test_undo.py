"""`todd undo`: going back one change at a time, without asking Claude."""

from todd import cli, db, intent, store
from todd.models import State

from .conftest import SLACK_DM
from .test_cli import ASK, saved, todd


def add(shell):
    result = todd("add", "reply to Priya about the Q3 numbers", SLACK_DM, "PLAT-412", "-q", ASK)
    assert result.exit_code == 0, result.output


def test_undo_goes_back_one_change_at_a_time(shell):
    add(shell)
    todd("start", "1", "-y")
    todd("wait", "1", "Priya", "to", "confirm", "-J")
    assert saved().state == State.WAITING

    result = todd("undo")
    assert result.exit_code == 0, result.output
    assert "Undid todd wait 1 Priya to confirm -J" in result.output
    assert saved().state == State.DOING

    todd("undo")
    assert saved().state == State.TODO

    result = todd("undo")
    assert "Undid todd add" in result.output
    assert store.tasks(db.connect(db.db_path())) == []

    result = todd("undo")
    assert result.exit_code == 1
    assert "nothing to undo" in result.output


def test_looking_leaves_nothing_to_undo(shell):
    add(shell)
    todd("ls")
    todd("show", "1")
    todd("undo")
    assert store.tasks(db.connect(db.db_path())) == []


def test_undo_says_what_stays_changed_in_jira(shell):
    add(shell)
    todd("review", "1", "-y")
    assert shell.transitions == [("PLAT-412", "In Review")]
    result = todd("undo")
    assert "outside todd" in result.output
    assert "PLAT-412 → In Review" in result.output
    assert saved().state == State.TODO
    assert shell.transitions == [("PLAT-412", "In Review")]  # Jira is left alone


def test_only_the_latest_changes_are_kept(shell, monkeypatch):
    monkeypatch.setattr(cli.undo, "KEEP", 2)
    add(shell)
    for words in ("one", "two", "three"):
        todd("note", "1", words)
    assert len(list(cli.undo.directory(db.db_path()).glob("*.sqlite"))) == 2


def test_claude_is_not_offered_undo():
    assert "todd undo" not in intent.reference(cli.command_group())
