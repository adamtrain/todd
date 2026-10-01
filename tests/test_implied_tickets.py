"""A pull request whose title names a Jira ticket in parentheses brings that ticket along."""

import copy

import pytest

from todd import links, triage
from todd.models import Link, LinkKind, Role

from .conftest import PR_URL, SOLO_PR_URL, answer, load
from .test_cli import saved, todd
from .test_projects import a_task
from .test_scenarios import link_roles


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("(PLAT-412) Move billing-worker to cluster-b", "PLAT-412"),
        ("fix(PLAT-412): move billing-worker to cluster-b", "PLAT-412"),
        ("Move billing-worker to cluster-b (PLAT-412)", "PLAT-412"),
        ("( PLAT-412 ) Move it", "PLAT-412"),
        ("Move (cluster-b) workers (PLAT-412)", "PLAT-412"),  # the first that is a key
        ("(PLAT-412) Move it (PLAT-999)", "PLAT-412"),
        ("(UTF-8) Fix the export encoding", "UTF-8"),  # looks like one; Jira decides
        ("Move billing-worker PLAT-412", None),  # not in parentheses
        ("[PLAT-412] Move it", None),
        ("(plat-412) Move it", None),  # not capitals
        ("(PLAT-412, PLAT-413) Move it", None),  # not exactly a key
        ("(PLAT) Move it", None),
        ("", None),
        (None, None),
    ],
)
def test_the_ticket_a_title_names(title, key):
    assert links.ticket_in_title(title) == key


def retitle(shell, number: int, title: str) -> None:
    shell.pulls[str(number)]["title"] = title


def ticket(shell, key: str, summary: str) -> None:
    made = copy.deepcopy(load("acli_view_PLAT-412"))
    made["key"], made["fields"]["summary"] = key, summary
    shell.tickets[key] = made


def test_a_pull_request_brings_the_ticket_its_title_names(shell):
    retitle(shell, 90, "(PLAT-412) Bump httpx")
    shell.answers.append(answer(title="Review the httpx bump", links=link_roles("deliverable")))
    result = todd("add", "review this", SOLO_PR_URL, "-y")
    assert result.exit_code == 0, result.output
    task = saved()
    assert [link.ref for link in task.links] == ["acme/billing#90", "PLAT-412"]
    implied = task.links[1]
    assert implied.kind == LinkKind.JIRA and implied.role == Role.TICKET
    assert implied.url == "https://acme.atlassian.net/browse/PLAT-412"
    assert (implied.title, implied.status) == (
        "Migrate billing workers to the new cluster",
        "In Progress",
    )
    line = "+ PLAT-412 · Migrate billing workers to the new cluster · In Progress"
    assert f"{line} · from the title of #90" in " ".join(result.output.split())
    assert (
        "Named in the title of pull request acme/billing#90: the ticket that tracks that pull "
        "request." in shell.prompts[0]
    )
    assert shell.transitions == []  # adding a task never changes Jira

    todd("done", "1", "-y")
    assert shell.transitions == [("PLAT-412", "Done")]


def test_something_in_parentheses_that_jira_does_not_know_is_left_out(shell):
    retitle(shell, 90, "(UTF-8) Fix the export encoding")
    shell.answers.append(answer(links=link_roles("deliverable")))
    out = todd("add", "review this", SOLO_PR_URL, "-y").output
    assert [link.ref for link in saved().links] == ["acme/billing#90"]
    assert "UTF-8" not in out.replace("(UTF-8) Fix the export encoding", "")


def test_a_ticket_in_one_of_your_projects_is_kept_even_when_jira_cannot_read_it(shell, config_file):
    config_file('[jira]\nsite = "acme.atlassian.net"\nkeys = ["OPS"]\n')
    retitle(shell, 90, "(OPS-9) Bump httpx")
    shell.answers.append(answer(links=link_roles("deliverable")))
    out = todd("add", "review this", SOLO_PR_URL, "-y").output
    assert [link.ref for link in saved().links] == ["acme/billing#90", "OPS-9"]
    assert "✗ OPS-9 · Couldn't read OPS-9 from Jira." in out


def test_a_ticket_you_linked_yourself_is_not_added_twice(shell):
    retitle(shell, 90, "(PLAT-412) Bump httpx")
    shell.answers.append(answer(links=link_roles("deliverable", "ticket")))
    out = todd("add", "review this", SOLO_PR_URL, "PLAT-412", "-y").output
    assert [link.ref for link in saved().links] == ["acme/billing#90", "PLAT-412"]
    assert "from the title of" not in out
    assert len([call for call in shell.ran("workitem") if "view" in call.argv]) == 1


def test_every_pull_request_in_a_stack_brings_its_ticket(shell):
    ticket(shell, "PLAT-413", "Cut over production")
    retitle(shell, 85, "(PLAT-412) Split billing worker config per cluster")
    retitle(shell, 86, "feat(PLAT-412): move billing-worker to cluster-b")
    retitle(shell, 88, "Cut production over to cluster-b (PLAT-413)")
    shell.answers.append(answer(links=link_roles(*["deliverable"] * 3, "ticket", "ticket")))
    todd("add", "land priya's stack", PR_URL, "-y")
    # The stack, bottom to top, then its tickets in the same order, each once.
    assert [link.ref for link in saved().links] == [
        "acme/billing#85",
        "acme/billing#86",
        "acme/billing#88",
        "PLAT-412",
        "PLAT-413",
    ]
    assert (
        "Jira ticket PLAT-413\nNamed in the title of pull request acme/billing#88"
        in (shell.prompts[0])
    )


def test_in_a_project_the_ticket_goes_with_its_pull_request(shell):
    retitle(shell, 90, "(PLAT-412) Bump httpx")
    shell.answers.append(
        answer(
            title="Upgrade httpx everywhere",
            links=[],
            tasks=[a_task("Land the httpx bump", [1], []), a_task("Tell the team", [], [1])],
        )
    )
    todd("add", "land this", SOLO_PR_URL, "then tell the team", "-y")
    assert saved(1).is_project and saved(1).links == []
    assert [link.ref for link in saved(2).links] == ["acme/billing#90", "PLAT-412"]


def test_owners_lets_claude_put_the_ticket_elsewhere():
    found = [
        Link(LinkKind.GITHUB, "u", ref="a/b#1", title="(PLAT-1) Do it"),
        Link(LinkKind.JIRA, None, ref="PLAT-1"),
    ]
    follows = triage.parse(
        answer(tasks=[a_task("One", [1], []), a_task("Two", [], [1])]),
        fallback_title="x",
        n_links=2,
    )
    assert triage.owners(follows, found) == {1: 1, 2: 1}
    elsewhere = triage.parse(
        answer(tasks=[a_task("One", [1], []), a_task("Two", [2], [1])]),
        fallback_title="x",
        n_links=2,
    )
    assert triage.owners(elsewhere, found) == {1: 1, 2: 2}


def test_pull_picks_up_a_ticket_added_to_the_title_later(shell):
    shell.answers.append(answer(links=link_roles("deliverable")))
    todd("add", "review this", SOLO_PR_URL, "-y")
    assert [link.ref for link in saved().links] == ["acme/billing#90"]
    retitle(shell, 90, "(PLAT-412) Bump httpx")
    out = todd("pull", "1", "--links-only").output
    assert "+ PLAT-412" in out and "from the title of #90" in out
    assert [link.ref for link in saved().links] == ["acme/billing#90", "PLAT-412"]
    assert saved().links[1].role == Role.TICKET
    assert "+ PLAT-412" not in todd("pull", "1", "--links-only").output  # only once


def test_doctor_says_which_ticket_a_title_names(shell):
    retitle(shell, 86, "(PLAT-412) Move billing-worker to cluster-b")
    assert "ticket in its title: PLAT-412" in todd("doctor", "--pr", PR_URL).output
