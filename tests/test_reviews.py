"""Who a task's pull requests are waiting on."""

from todd import github, render, reviews
from todd.models import Link, LinkKind, Reviewer, ReviewState, State, Task
from todd.people import Nicknames

from .conftest import load

R = ReviewState


def pull(number: int, status: str, *reviewers: Reviewer) -> Link:
    return Link(
        LinkKind.GITHUB,
        f"https://github.com/acme/billing/pull/{number}",
        ref=f"acme/billing#{number}",
        status=status,
        reviewers=list(reviewers),
    )


def test_a_pending_request_beats_an_earlier_review():
    node = dict(load("gh_pulls")["86"])
    node["reviewRequests"] = {
        "nodes": [{"requestedReviewer": {"__typename": "User", "login": "sam-k"}}]
    }
    pr = github.parse_pull(node, "acme", "billing")
    pr.viewer = "adamtrain"
    states = {r.login: (r.state, r.you) for r in pr.reviewers}
    assert states == {"sam-k": (R.REQUESTED, False), "adamtrain": (R.COMMENTED, True)}


def test_teams_are_marked_and_the_author_is_left_out():
    node = dict(load("gh_pulls")["86"])
    node["latestReviews"] = {"nodes": [{"author": {"login": "priya-n"}, "state": "COMMENTED"}]}
    pr = github.parse_pull(node, "acme", "billing")
    assert [(r.login, r.team) for r in pr.reviewers] == [("acme/platform-reviewers", True)]


def test_the_board_counts_open_pull_requests_only():
    links = [
        pull(85, "merged", Reviewer("sam", R.REQUESTED)),
        pull(86, "open · needs review", Reviewer("sam", R.REQUESTED), Reviewer("luke", R.APPROVED)),
        pull(88, "draft", Reviewer("sam", R.REQUESTED), Reviewer("nik", R.CHANGES_REQUESTED)),
        pull(90, "open", Reviewer("adamtrain", R.REQUESTED, you=True)),
    ]
    board = reviews.board(links)
    assert board.waiting == {"sam": ["#86", "#88"]}
    assert board.approved == {"luke": ["#86"]}
    assert board.changes == {"nik": ["#88"]}
    assert board.yours == ["#90"]


def test_waiting_summary_names_the_busiest_and_counts_the_rest():
    links = [
        pull(1, "open", Reviewer("nik", R.REQUESTED), Reviewer("will", R.REQUESTED)),
        pull(2, "open", Reviewer("nik", R.REQUESTED), Reviewer("ana", R.REQUESTED)),
        pull(
            3,
            "open",
            Reviewer("nik", R.REQUESTED),
            Reviewer("bo", R.REQUESTED),
            Reviewer("acme/infra", R.REQUESTED, team=True),
        ),
    ]
    task = Task("t", state=State.WAITING, links=links)
    names = Nicknames(names={"nik": "Nik", "acme/infra": "Infra"})
    assert render.waiting_summary(task, names) == "Nik (3), ana, bo +2 more"
    assert render.waiting_summary(task, names, limit=99) == "Nik (3), ana, bo, Infra, will"
    assert render.waiting_summary(Task("t"), names) is None
