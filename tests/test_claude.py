import json

import pytest

from todd.claude import Claude
from todd.config import ClaudeConfig
from todd.errors import ToddError
from todd.proc import Result

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_argv_is_a_single_tool_free_turn():
    argv = Claude(ClaudeConfig(model="opus", effort="low", args=["--safe-mode"])).argv(
        "be brief", SCHEMA
    )
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--system-prompt") + 1] == "be brief"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "low"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert "--no-session-persistence" in argv
    assert argv[-1] == "--safe-mode"


def test_defaults_leave_model_to_claude_code():
    argv = Claude(ClaudeConfig(effort=None)).argv("s")
    assert "--model" not in argv
    assert "--effort" not in argv
    assert "--json-schema" not in argv


def test_structured_answer_and_prompt_on_stdin(shell):
    shell.answers.append({"ok": True})
    assert Claude().structured("the prompt", system="s", schema=SCHEMA) == {"ok": True}
    assert shell.prompts == ["the prompt"]


def test_falls_back_to_json_in_the_result_text(shell):
    shell.answers.append(Result(0, json.dumps({"is_error": False, "result": '{"ok": true}'}), ""))
    assert Claude().structured("p", system="s", schema=SCHEMA) == {"ok": True}


def test_text_answer(shell):
    shell.answers.append("  Sent them over, see the sheet.  ")
    assert Claude().text("p", system="s") == "Sent them over, see the sheet."


FAILURES = [
    (Result(1, "", "Invalid API key · Please run /login"), "didn't answer"),
    (
        Result(0, json.dumps({"is_error": True, "result": "Credit balance is too low"}), ""),
        "couldn't finish",
    ),
    (
        Result(0, json.dumps({"is_error": False, "result": "no json here"}), ""),
        "structured answer",
    ),
    (Result(0, "not json", ""), "wasn't JSON"),
]


@pytest.mark.parametrize(("result", "complaint"), FAILURES)
def test_failures_are_explained(shell, result, complaint):
    shell.answers.append(result)
    with pytest.raises(ToddError, match=complaint):
        Claude().structured("p", system="s", schema=SCHEMA)


def test_missing_claude(shell):
    shell.missing.add("claude")
    with pytest.raises(ToddError) as e:
        Claude().text("p", system="s")
    assert "Install Claude Code" in (e.value.hint or "")


def verbose(envelope: dict) -> list[dict]:
    """How `claude -p --output-format json` answers in verbose mode: every message, result last.
    The shape is from a real run of Claude Code 2.1.281 with --verbose."""
    return [
        {"type": "system", "subtype": "init", "session_id": "s", "tools": [], "model": "m"},
        {"type": "assistant", "message": {"role": "assistant", "content": []}},
        {"type": "user", "message": {"role": "user", "content": []}},
        {"type": "rate_limit_event"},
        {"type": "result", "subtype": "success", **envelope},
    ]


def test_verbose_mode_replies_are_understood(shell):
    envelope = {"is_error": False, "result": '{"ok": true}', "structured_output": {"ok": True}}
    shell.answers.append(Result(0, json.dumps(verbose(envelope)), ""))
    assert Claude().structured("p", system="s", schema=SCHEMA) == {"ok": True}


def test_verbose_mode_errors_are_still_errors(shell):
    envelope = {"is_error": True, "result": "Credit balance is too low"}
    shell.answers.append(Result(0, json.dumps(verbose(envelope)), ""))
    with pytest.raises(ToddError, match="couldn't finish") as e:
        Claude().structured("p", system="s", schema=SCHEMA)
    assert "Credit balance" in (e.value.detail or "")


@pytest.mark.parametrize("reply", ["[]", '[{"type": "system"}]', "42", '"ok"'])
def test_an_unexpected_reply_says_what_it_was(shell, reply):
    shell.answers.append(Result(0, reply, ""))
    with pytest.raises(ToddError, match="wasn't what todd expected") as e:
        Claude().structured("p", system="s", schema=SCHEMA)
    assert e.value.detail == f"It sent: {reply}"
