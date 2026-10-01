import json

import pytest

from todd import github
from todd.errors import ToddError
from todd.links import github_item
from todd.proc import Result

from .conftest import PR_URL, load


def test_parse_stack_keeps_bottom_to_top_order():
    stack = github.parse_stack(load("gh_stacks"), "acme", "billing")
    assert stack is not None
    assert (stack.number, stack.trunk, stack.open, stack.numbers) == (
        17,
        "main",
        True,
        [85, 86, 88],
    )
    assert stack.key == "acme/billing/stacks/17"
    assert github.parse_stack([], "acme", "billing") is None


def test_stack_lookup_uses_the_stacks_api(shell):
    item = github_item(PR_URL)
    assert item is not None
    github.stack_of(item)
    (call,) = shell.ran("api")
    assert call.argv[-1] == "repos/acme/billing/stacks?pull_request=86"
    assert f"X-GitHub-Api-Version: {github.STACKS_API_VERSION}" in call.argv


def test_one_query_for_several_pull_requests():
    query = github.pulls_query([85, 86])
    assert "pr85: pullRequest(number: 85) { ...PR }" in query
    assert "pr86: pullRequest(number: 86) { ...PR }" in query
    assert "fragment PR on PullRequest" in query


@pytest.mark.parametrize(
    ("number", "status"),
    [
        ("85", "merged"),
        ("86", "open · changes requested"),
        ("88", "draft"),
        ("90", "open · approved · checks failing"),
    ],
)
def test_status_in_a_few_words(number, status):
    pr = github.parse_pull(load("gh_pulls")[number], "acme", "billing")
    assert pr.status == status


def test_parse_pull_keeps_what_reviewers_said():
    pr = github.parse_pull(load("gh_pulls")["86"], "acme", "billing")
    assert pr.author == "priya-n"
    assert [(r.author, r.state) for r in pr.latest_reviews] == [
        ("sam-k", "CHANGES_REQUESTED"),
        ("adamtrain", "COMMENTED"),
    ]
    assert [r.author for r in pr.reviews] == ["sam-k"]  # only reviews that said something
    assert pr.requested == ["acme/platform-reviewers"]
    assert [(t.path, t.author) for t in pr.open_threads] == [("deploy/worker.yaml", "sam-k")]
    assert pr.comments[0].body.startswith("@adamtrain could you take a look")


def test_long_text_is_clipped():
    node = dict(load("gh_pulls")["90"], body="x" * 10_000)
    pr = github.parse_pull(node, "acme", "billing")
    assert pr.body is not None
    assert len(pr.body) == github.BODY_LIMIT + 1 and pr.body.endswith("…")


def test_partial_answers_are_used(shell):
    pulls = github.pulls("acme", "billing", [86, 404])
    assert [pr.number for pr in pulls] == [86]


def test_nothing_found_is_an_error(shell):
    with pytest.raises(ToddError, match="didn't return acme/billing#404") as e:
        github.pulls("acme", "billing", [404])
    assert "Could not resolve" in (e.value.detail or "")


def test_gh_failure_without_data_is_explained(shell, monkeypatch):
    monkeypatch.setattr(
        github.proc, "run", lambda *a, **k: Result(1, "", "gh: To get started, run gh auth login")
    )
    with pytest.raises(ToddError, match="Couldn't read acme/billing pull requests") as e:
        github.pulls("acme", "billing", [86])
    assert "gh auth login" in (e.value.detail or "")


def test_stack_membership_is_numbered(shell):
    context = github.pull_request(PR_URL)
    assert context.stack is not None
    assert [(pr.number, pr.stack_position) for pr in context.pulls] == [(85, 1), (86, 2), (88, 3)]


def test_not_a_pull_request():
    with pytest.raises(ToddError, match="isn't a GitHub pull request"):
        github.pull_request("https://github.com/acme/billing/issues/7")


def test_the_real_stack_shape_parses():
    # Trimmed from a real answer to GET /repos/github/gh-stack/stacks (2026-09-30).
    real = json.loads(
        '[{"id":1522757,"number":530,"node_id":"PRS_kwDORKR9iM4AFzxF",'
        '"url":"https://api.github.com/repos/github/gh-stack/stacks/530","base":{"ref":"main"},'
        '"open":true,"created_at":"2026-09-28T17:20:15Z","pull_requests":['
        '{"number":526,"state":"open","draft":false,"merged_at":null,"head":{"ref":"a","sha":"1"}},'
        '{"number":527,"state":"open","draft":false,"merged_at":null,"head":{"ref":"b","sha":"2"}},'
        '{"number":521,"state":"open","draft":false,"merged_at":null,"head":{"ref":"c","sha":"3"}}]}]'
    )
    stack = github.parse_stack(real, "github", "gh-stack")
    assert stack is not None
    assert stack.numbers == [526, 527, 521]  # stack order, not number order
