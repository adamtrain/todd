"""`todd "…"`: what you say, turned into todd commands by Claude, shown, then run."""

from todd import cli, intent
from todd.models import State
from todd.people import Nicknames

from .conftest import SLACK_DM, answer
from .test_cli import named, saved, todd


def plan(
    *steps: tuple[list[str], str], question: str | None = None, then: str | None = None
) -> dict:
    return {
        "steps": [{"argv": argv, "says": says} for argv, says in steps],
        "question": question,
        "then": then,
    }


WEBHOOK, RUNBOOK = "Ship the Torii webhook", "Write the Torii runbook"


def two_tasks(shell) -> None:
    """#1 Ship the Torii webhook, #2 Write the Torii runbook."""
    shell.answers += [
        answer(title="Ship the Torii webhook", links=[]),
        answer(title="Write the Torii runbook", links=[]),
    ]
    todd("add", "ship the torii webhook", "-y")
    todd("add", "write the torii runbook", "-y")


def test_saying_it_runs_the_commands_claude_works_out(shell, picks):
    two_tasks(shell)
    shell.answers.append(
        plan(
            (["done", "1", "-J"], "Finish #1 Ship the Torii webhook"),
            (["start", "2", "-J"], "Start #2 Write the Torii runbook"),
        )
    )
    result = todd("move the current Torii task to done and start the next one")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "todd will" in out and "Finish #1 Ship the Torii webhook" in out
    assert "todd done 1 -J" in out
    assert picks.asked[-1][:2] == ("Do it?", ["do", "change", "cancel"])
    assert picks.asked[-1][2] == "do"  # the highlight starts on Do it
    assert "▶ 1/2 Finish #1" in out and "▶ 2/2 Start #2" in out
    assert (named(WEBHOOK).state, named(RUNBOOK).state) == (State.DONE, State.DOING)
    # The plan's steps use the numbers as they were; numbers only settle once it has run.
    assert out.index("#2 to do → doing") < out.index("Renumbered: #1 is now #2 · #2 is now #1")
    assert (named(WEBHOOK).id, named(RUNBOOK).id) == (2, 1)

    request = shell.prompts[-1]
    assert (
        "<request>move the current Torii task to done and start the next one</request>" in request
    )
    assert "#1 [to do] Ship the Torii webhook" in request
    assert "todd done TASK_ID [WORDS...]" in request


def test_unquoted_words_work_too_even_when_they_start_with_a_command(shell, picks):
    two_tasks(shell)
    shell.answers.append(plan((["show", "2"], "Show #2 Write the Torii runbook")))
    out = todd("show", "me", "the", "runbook", "task").output
    assert "<request>show me the runbook task</request>" in shell.prompts[-1]
    assert "Write the Torii runbook" in out


def test_commands_are_still_commands(shell):
    two_tasks(shell)
    asked = len(shell.prompts)
    assert "Ship the Torii webhook" in todd("show", "1").output
    assert len(shell.prompts) == asked  # no Claude


def test_looking_runs_without_asking(shell, picks):
    two_tasks(shell)
    shell.answers.append(plan((["ls", "--area", "platform"], "List your platform tasks")))
    out = todd("what's on my platform plate?").output
    assert "Ship the Torii webhook" in out
    assert all(q != "Do it?" for q, _, _ in picks.asked)


def test_claude_asks_when_it_cannot_tell(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan(question="Which Torii task: #1 Ship the webhook, or #2 Write the runbook?"),
        plan((["done", "2", "-J"], "Finish #2 Write the Torii runbook")),
    ]
    out = todd("finish the torii thing", input="the runbook\n").output
    assert "? Which Torii task" in out
    assert "You asked: Which Torii task" in shell.prompts[-1]
    assert "<answer>the runbook</answer>" in shell.prompts[-1]
    assert saved(2).state == State.DONE


def test_changing_the_plan(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan((["done", "1", "-J"], "Finish #1")),
        plan((["drop", "1", "-J"], "Drop #1")),
    ]
    picks.script += ["change", "do"]
    todd("finish the webhook", input="actually drop it, we're not shipping it\n")
    assert "<previous_plan>" in shell.prompts[-1]
    assert "<change>actually drop it, we're not shipping it</change>" in shell.prompts[-1]
    assert named(WEBHOOK).state == State.DROPPED


def test_cancel_does_nothing(shell, picks):
    two_tasks(shell)
    shell.answers.append(plan((["done", "1"], "Finish #1")))
    picks.script.append("cancel")
    assert "Nothing done." in todd("finish the webhook").output
    assert saved(1).state == State.TODO


def test_a_plan_that_does_not_parse_goes_back_once(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan((["done", "the-webhook"], "Finish the webhook")),
        plan((["done", "1", "-J"], "Finish #1")),
    ]
    todd("finish the webhook")
    assert "Some of those command lines don't work in todd:" in shell.prompts[-1]
    assert "not a valid int" in shell.prompts[-1]
    assert named(WEBHOOK).state == State.DONE


def test_a_plan_that_still_does_not_parse_does_nothing(shell, picks):
    two_tasks(shell)
    shell.answers += [plan((["explode", "1"], "Boom"))] * 2
    result = todd("blow it up")
    assert result.exit_code == 1
    assert "Claude's plan has commands todd can't run" in result.output
    assert "todd has no command 'explode'" in result.output


def test_without_a_terminal_changes_need_yes(shell):
    two_tasks(shell)
    shell.answers.append(plan((["done", "1", "-J"], "Finish #1")))
    result = todd("finish the webhook")
    assert result.exit_code == 1 and "Nothing done" in result.output
    assert saved(1).state == State.TODO
    shell.answers.append(plan((["done", "1", "-J"], "Finish #1")))
    todd("do", "-y", "finish the webhook")
    assert named(WEBHOOK).state == State.DONE


def test_a_failing_step_stops_the_rest(shell, picks):
    two_tasks(shell)
    shell.answers.append(plan((["done", "99"], "Finish #99"), (["start", "2", "-J"], "Start #2")))
    result = todd("finish 99 then start the runbook")
    assert "There's no task #99" in result.output
    assert "Stopped after step 1" in result.output
    assert saved(2).state == State.TODO


def test_something_new_becomes_a_capture(shell, picks):
    shell.answers += [
        plan((["add", "call Mike about the RFC", SLACK_DM], "Add a task: call Mike about the RFC")),
        answer(title="Call Mike about the RFC", links=[]),
    ]
    todd("remind me to call Mike about the RFC", SLACK_DM, input="\n")
    assert saved(1).title == "Call Mike about the RFC"


def test_parse_tidies_claudes_answer():
    parsed = intent.parse(
        {"steps": [{"argv": ["todd", "done", "1"], "says": "Finish #1"}, {"argv": []}, "junk"]}
    )
    assert [s.argv for s in parsed.steps] == [["done", "1"]]
    assert parsed.question is None
    assert intent.Step(["add", "call Mike", "#3"], "x").command_line == 'todd add "call Mike" "#3"'


def test_which_steps_only_look():
    looks = intent.Step(["ls", "--area", "x"], "")
    assert looks.looks_only
    assert intent.Step(["followup"], "").looks_only
    assert not intent.Step(["followup", "done", "1"], "").looks_only
    assert intent.Step(["config"], "").looks_only
    assert not intent.Step(["config", "--init"], "").looks_only
    assert not intent.Step(["done", "1"], "").looks_only


def test_the_reference_comes_from_the_cli_itself():
    group = cli.command_group()
    reference = intent.reference(group)
    assert "todd move TASK_ID TO [WORDS...]" in reference
    assert "--priority/-P {urgent|high|normal|low}" in reference
    assert "todd followup add TASK_ID ACTION..." in reference
    assert not any(line.startswith("todd do ") for line in reference.splitlines())


def test_check_catches_what_would_not_run():
    group = cli.command_group()
    assert intent.check(group, ["done", "1"]) is None
    assert intent.check(group, ["followup"]) is None
    assert "not a valid int" in (intent.check(group, ["done", "x"]) or "")
    assert intent.check(group, ["do", "x"]) == "todd has no command 'do'"
    assert intent.check(group, ["edit", "1", "--bogus"]) == "No such option: --bogus"
    assert intent.check(group, ["done", "--help"]) == "asks for help instead of doing something"


def test_context_lists_what_claude_can_refer_to(shell, tmp_path):
    from todd import db

    two_tasks(shell)
    todd("followup", "add", "1", "Tell", "Theo", "--when", "done")
    todd("nick", "niik", "Nik")
    conn = db.connect(db.db_path())
    from .conftest import TODAY

    text = intent.context(conn, TODAY, Nicknames(names={"niik": "Nik"}))
    assert "Tasks:\n#1 [to do] Ship the Torii webhook" in text
    assert "↪1 Tell Theo (task #1, when done)" in text
    assert "@niik is Nik" in text


def test_claude_is_told_what_the_states_mean(shell, picks):
    two_tasks(shell)
    shell.answers.append(plan((["now"], "Show what you can act on")))
    out = todd("what can I work on right now?").output
    assert "Ship the Torii webhook" in out
    assert all(q != "Do it?" for q, _, _ in picks.asked)  # looking needs no go-ahead
    request = shell.prompts[-1]
    assert "<states>" in request and "- in_review (in review): Your work is done" in request
    assert "Blocked: Not a state of its own" in request
    assert "Following: Following is for things that aren't yours (yet)" in request
    assert "todd now  What you can act on now" in request and "todd states  Every state" in request


def test_context_gives_projects_their_derived_state_and_marks_blocked_tasks(shell):
    from todd import db

    from .conftest import TODAY
    from .test_projects import a_task

    shell.answers.append(
        answer(
            title="Torii",
            links=[],
            tasks=[a_task("Ship it", [], [], track="waiting"), a_task("Document it", [], [1])],
        )
    )
    todd("add", "ship torii, then document it", "-y")
    text = intent.context(db.connect(db.db_path()), TODAY, Nicknames())
    assert "#1 [project · waiting] Torii · tasks in order: #2 (waiting), #3 (blocked)" in text
    assert "#3 [blocked] Document it · in project #1 “Torii”" in text
    todd("done", "2", "-J")
    todd("done", "2", "-J")  # "Document it" moved down to #2
    text = intent.context(db.connect(db.db_path()), TODAY, Nicknames())
    assert "[project" not in text  # a finished project isn't something to move


# ── More than one round ────────────────────────────────────────────────────


def test_claude_comes_back_for_steps_that_depend_on_earlier_ones(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan(
            (["add", "audit the Torii logs", "-y"], "Add a task: audit the Torii logs"),
            then="make it wait on the runbook and start it",
        ),
        answer(title="Audit the Torii logs", links=[]),
        plan(
            (["block", "3", "--on", "2"], "Make #3 Audit the Torii logs wait on #2"),
            (["start", "3", "-J"], "Start #3 Audit the Torii logs"),
        ),
    ]
    result = todd("add a task to audit the torii logs, make it wait on the runbook, and start it")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "then: make it wait on the runbook and start it" in out
    assert out.index("todd will") < out.index("Filed #3") < out.index("Next, todd will")
    audit = named("Audit the Torii logs")
    assert audit.state == State.DOING and [b.id for b in audit.blockers] == [2]
    # You're asked about each round of steps, starting on Do it.
    asked = [(question, default) for question, _, default in picks.asked if question == "Do it?"]
    assert asked == [("Do it?", "do"), ("Do it?", "do")]

    again = shell.prompts[-1]
    assert "<done>" in again and '- todd add "audit the Torii logs" -y (Add a task' in again
    assert "You said you would then: make it wait on the runbook and start it" in again
    assert "#3 [to do] Audit the Torii logs" in again  # the tasks as they are now
    assert "Give the steps that are left." in again


def test_a_later_round_can_have_nothing_left_to_do(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan((["start", "1", "-J"], "Start #1"), then="check whether more is needed"),
        plan(),
    ]
    result = todd("start the webhook and anything it needs")
    assert result.exit_code == 0 and named(WEBHOOK).state == State.DOING
    assert "didn't find anything" not in result.output


def test_you_can_stop_between_rounds(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan((["start", "1", "-J"], "Start #1"), then="finish the runbook"),
        plan((["done", "2", "-J"], "Finish #2")),
    ]
    picks.script += ["do", "cancel"]
    out = todd("start the webhook then finish the runbook").output
    assert picks.asked[-1][1] == ["do", "change", "cancel"]
    assert "Nothing more done." in out
    assert (named(WEBHOOK).state, named(RUNBOOK).state) == (State.DOING, State.TODO)


def test_rounds_do_not_go_on_for_ever(shell, picks):
    two_tasks(shell)
    shell.answers += [plan((["show", "1"], "Look at #1"), then="look again")] * cli.MAX_ROUNDS
    out = todd("keep looking at the webhook").output
    assert f"that's {cli.MAX_ROUNDS} rounds of steps" in out
    assert len([p for p in shell.prompts if "<request>keep looking" in p]) == cli.MAX_ROUNDS


def test_a_failed_step_ends_the_whole_request(shell, picks):
    two_tasks(shell)
    shell.answers += [
        plan((["done", "99"], "Finish #99"), then="start the runbook"),
        plan((["start", "2", "-J"], "Start #2")),
    ]
    result = todd("finish 99 then start the runbook")
    assert result.exit_code == 1 and named(RUNBOOK).state == State.TODO
    assert len(shell.answers) == 1  # Claude wasn't asked for the rest


def test_the_schema_asks_what_comes_after():
    assert intent.SCHEMA["required"] == ["steps", "question", "then"]
    assert intent.parse(plan((["ls"], "List"), then=" more ")).then == "more"
    assert intent.parse(plan(then="more")).then is None  # nothing was run, so nothing follows
