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
