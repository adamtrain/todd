"""Follow-ups: when they fire, when they stop mattering, and how they're kept."""

from datetime import date, timedelta

import pytest

from todd import db, effects, store, triage
from todd.config import Config
from todd.models import Followup, FollowupStatus, Link, LinkKind, Role, State, Task, parse_state

from .conftest import SLACK_DM, TODAY, answer


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def fu(action: str = "Tell Theo", **kw) -> Followup:
    return Followup(action=action, **kw)


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("in-review", State.IN_REVIEW),
        ("in review", State.IN_REVIEW),
        ("review", State.IN_REVIEW),
        ("To Do", State.TODO),
        ("follow", State.FOLLOWING),
        ("done", State.DONE),
        ("nope", None),
    ],
)
def test_states_as_people_type_them(text, state):
    assert parse_state(text) == state


# ── Firing and going moot ──────────────────────────────────────────────────


def test_a_when_done_follow_up_fires_on_done():
    task = Task("t", followups=[fu(when=State.DONE)])
    changes = effects.followups_on_move(task, State.DOING, State.DONE)
    assert [f.action for f in changes.fired] == ["Tell Theo"]
    assert effects.followups_on_move(task, State.TODO, State.DOING).fired == []


def test_skipping_past_a_state_still_fires_it():
    ask = fu("Ask Mike to review", when=State.IN_REVIEW)
    changes = effects.followups_on_move(Task("t", followups=[ask]), State.DOING, State.DONE)
    assert changes.fired == [ask]


def test_a_dated_warning_stops_mattering_once_the_task_gets_there():
    warn = fu("Tell Mike review slips", due=TODAY, unless=State.IN_REVIEW)
    task = Task("t", followups=[warn])
    assert effects.followups_on_move(task, State.TODO, State.DOING).moot == []
    assert effects.followups_on_move(task, State.DOING, State.IN_REVIEW).moot == [warn]
    assert effects.followups_on_move(task, State.DOING, State.DONE).moot == [warn]


def test_dropping_a_task_leaves_follow_ups_alone():
    task = Task("t", followups=[fu(when=State.DONE), fu(due=TODAY, unless=State.DONE)])
    changes = effects.followups_on_move(task, State.DOING, State.DROPPED)
    assert changes.fired == [] and changes.moot == []


def test_closed_follow_ups_are_ignored():
    done = fu(when=State.DONE, status=FollowupStatus.DONE)
    assert (
        effects.followups_on_move(Task("t", followups=[done]), State.DOING, State.DONE).fired == []
    )


def test_is_due():
    assert fu(due=TODAY).is_due(TODAY)
    assert fu(due=TODAY - timedelta(days=2)).is_due(TODAY)
    assert not fu(due=TODAY + timedelta(days=1)).is_due(TODAY)
    assert not fu(when=State.DONE).is_due(TODAY)
    assert not fu(due=TODAY, status=FollowupStatus.DONE).is_due(TODAY)


# ── Storing ────────────────────────────────────────────────────────────────


def test_follow_ups_round_trip(conn):
    task_id = store.add(conn, Task("Write the RFC"))
    store.add_followup(
        conn,
        task_id,
        fu("Ask Mike R to review", person="Mike R", when=State.IN_REVIEW, by_you=True),
    )
    with db.tx(conn):
        store.insert_followup(
            conn, task_id, fu("Tell Mike R it slips", due=date(2026, 10, 2), unless=State.IN_REVIEW)
        )
    task = store.get(conn, task_id)
    ask, warn = task.followups
    assert (ask.person, ask.when, ask.by_you) == ("Mike R", State.IN_REVIEW, True)
    assert (warn.due, warn.unless, warn.by_you) == (date(2026, 10, 2), State.IN_REVIEW, False)
    assert task.entries[-1].text == "↪ Added: Ask Mike R to review"
    assert [f.action for f in store.open_followups(conn)] == [
        "Tell Mike R it slips",
        "Ask Mike R to review",
    ]


def test_closing_and_rescheduling(conn):
    task_id = store.add(conn, Task("t"))
    followup_id = store.add_followup(conn, task_id, fu(when=State.DONE))
    store.reschedule_followup(conn, followup_id, TODAY)
    assert store.get_followup(conn, followup_id).due == TODAY
    assert store.get_followup(conn, followup_id).when is None
    closed = store.close_followup(conn, followup_id, FollowupStatus.DONE)
    assert closed.status == FollowupStatus.DONE
    assert store.open_followups(conn) == []
    assert store.get(conn, task_id).entries[-1].text == "↪ Done: Tell Theo"


def test_refiling_replaces_claudes_open_follow_ups_but_keeps_yours_and_closed_ones(conn):
    task_id = store.add(conn, Task("t"))
    mine = store.add_followup(conn, task_id, fu("Mine", by_you=True))
    with db.tx(conn):
        old = store.insert_followup(conn, task_id, fu("Claude's old one"))
        finished = store.insert_followup(conn, task_id, fu("Claude's finished one"))
    store.close_followup(conn, finished, FollowupStatus.DONE)
    with db.tx(conn):
        store.replace_claudes_followups(conn, task_id, [fu("Claude's new one")])
    actions = {f.action for f in store.get(conn, task_id).followups}
    assert actions == {"Mine", "Claude's finished one", "Claude's new one"}
    assert old not in {f.id for f in store.get(conn, task_id).followups}
    assert mine in {f.id for f in store.get(conn, task_id).followups}


def test_people_filter_finds_follow_up_people(conn):
    task_id = store.add(conn, Task("t"))
    store.add_followup(conn, task_id, fu(person="Theo A"))
    assert [t.id for t in store.tasks(conn, person="theo")] == [task_id]


# ── Claude's answer ────────────────────────────────────────────────────────


def test_parse_follow_ups_and_track():
    filing = triage.parse(
        answer(
            track="waiting",
            follow_ups=[
                {
                    "action": "Tell Mike R it slips",
                    "person": "Mike R",
                    "when": None,
                    "due": "2026-10-02",
                    "unless": "in_review",
                },
                {
                    "action": "Ask Mike R to review",
                    "person": "Mike R",
                    "when": "in_review",
                    "due": None,
                    "unless": None,
                },
                {"action": "  ", "person": None, "when": None, "due": None, "unless": None},
                {
                    "action": "Bad date",
                    "person": None,
                    "when": "someday",
                    "due": "friday",
                    "unless": None,
                },
            ],
        ),
        fallback_title="x",
        n_links=2,
    )
    assert filing.track == State.WAITING
    warn, ask, bad = filing.followups
    assert (warn.due, warn.unless) == (date(2026, 10, 2), State.IN_REVIEW)
    assert ask.when == State.IN_REVIEW
    assert (bad.when, bad.due) == (None, None)


def test_a_missing_title_means_asking_the_person():
    filing = triage.parse(
        answer(title=None, needs_title=True), fallback_title="your words", n_links=0
    )
    assert filing.needs_title
    assert filing.title == "your words"
    # A title with no explicit flag is fine; no title at all always needs one.
    assert not triage.parse(answer(), fallback_title="x", n_links=0).needs_title
    assert triage.parse(answer(title=None), fallback_title="x", n_links=0).needs_title


def test_following_always_gets_a_dated_check_in():
    check_in = TODAY + timedelta(days=14)
    filing = triage.parse(
        answer(track="following", title="Keep an eye on the ledger refactor", follow_ups=[]),
        fallback_title="x",
        n_links=0,
        check_in=check_in,
    )
    assert filing.track == State.FOLLOWING
    (check,) = filing.followups
    assert (check.action, check.due) == ("Check on Keep an eye on the ledger refactor", check_in)


def test_following_keeps_claudes_own_check_in():
    dated = {
        "action": "Check in with Dana",
        "person": "Dana",
        "when": None,
        "due": "2026-10-09",
        "unless": None,
    }
    filing = triage.parse(
        answer(track="following", follow_ups=[dated]),
        fallback_title="x",
        n_links=0,
        check_in=TODAY + timedelta(days=14),
    )
    assert [f.action for f in filing.followups] == ["Check in with Dana"]


def test_the_prompt_gives_the_check_in_date_and_what_the_person_already_has(shell):
    task = Task(
        "t",
        description="following the ledger refactor",
        followups=[fu("Mine", by_you=True), fu("Claude's old", status=FollowupStatus.OPEN)],
    )
    text = triage.prompt(
        task, [], areas={}, used=[], today=TODAY, check_in=TODAY + timedelta(days=14)
    )
    assert "The default check-in date is Wednesday 2026-10-14." in text
    assert "- Mine (open)" in text
    assert "Claude's old" not in text
    assert "follow_ups" in triage.SYSTEM and "needs_title" in triage.SYSTEM


def test_applying_keeps_roles_you_set(shell, conn):
    link = Link(LinkKind.SLACK, SLACK_DM, role=Role.RESPOND, role_fixed=True)
    task = Task("t", links=[link])
    store.add(conn, task)
    filing = triage.parse(
        answer(links=[{"index": 1, "role": "reference", "note": "background", "author": None}]),
        fallback_title="t",
        n_links=1,
    )
    triage.apply(conn, task, filing, task.links)
    saved = store.get(conn, task.id)  # ty: ignore[invalid-argument-type]
    assert (saved.links[0].role, saved.links[0].note) == (Role.RESPOND, "background")


def test_applying_a_followed_item(shell, conn):
    task = Task("t")
    store.add(conn, task)
    filing = triage.parse(answer(track="following"), fallback_title="t", n_links=0, check_in=TODAY)
    assert triage.apply(conn, task, filing, []).state == State.FOLLOWING
    saved = store.get(conn, task.id)  # ty: ignore[invalid-argument-type]
    assert saved.state == State.FOLLOWING
    assert saved.followups[0].due == TODAY


def test_config_check_in_days(config_file):
    from todd import config

    assert config.load(config_file("[following]\ncheck_in_days = 7\n")).following.check_in_days == 7
    with pytest.raises(Exception, match="at least 1"):
        config.load(config_file("[following]\ncheck_in_days = 0\n"))
    loaded = config.load(config_file('[jira.status]\nfollowing = "Watching"\n'))
    assert loaded.jira.target("X-1", State.FOLLOWING) == "Watching"


def test_every_parsed_state_has_a_label_and_progress():
    for state in State:
        assert state.label and state.progress in range(4)
    assert Config().following.check_in_days == 14
