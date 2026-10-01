import pytest

from todd import jira
from todd.errors import ToddError

from .conftest import load


def test_parses_acli_json_in_jiras_rest_shape():
    ticket = jira.parse_ticket(load("acli_view_PLAT-412"), key="PLAT-412")
    assert ticket.key == "PLAT-412"
    assert ticket.summary == "Migrate billing workers to the new cluster"
    assert ticket.status == "In Progress"
    assert ticket.type == "Story"
    assert ticket.assignee == "Adam Train"
    assert ticket.priority == "High"
    # No site configured, so the browse URL comes from the API URL's host.
    assert ticket.url == "https://acme.atlassian.net/browse/PLAT-412"


def test_description_is_flattened_from_atlassian_document_format():
    ticket = jira.parse_ticket(load("acli_view_PLAT-412"), key="PLAT-412")
    assert ticket.description == (
        "Move the billing-worker deployment to the new cluster. @Priya Nair needs the Q3 "
        "numbers first.\n\n"
        "- Cut over staging\n"
        "- Cut over production\n\n"
        "Runbook: https://acme.atlassian.net/wiki/x/runbook"
    )


def test_tolerates_sparse_or_flat_output():
    ticket = jira.parse_ticket(
        {"key": "OPS-9", "summary": "Rotate keys", "status": "To Do", "description": "plain"},
        key="OPS-9",
        site="acme.atlassian.net",
    )
    assert (ticket.summary, ticket.status, ticket.description) == ("Rotate keys", "To Do", "plain")
    assert ticket.url == "https://acme.atlassian.net/browse/OPS-9"
    assert jira.parse_ticket([{"key": "X-1", "fields": {}}], key="X-1").summary is None


def test_view_runs_acli(shell):
    ticket = jira.Jira().view("PLAT-412")
    assert ticket.status == "In Progress"
    assert shell.calls[0].argv == [
        "acli",
        "jira",
        "workitem",
        "view",
        "PLAT-412",
        "--json",
        "--fields",
        jira.FIELDS,
    ]


def test_view_failure_is_explained(shell):
    with pytest.raises(ToddError, match="Couldn't read NOPE-1") as e:
        jira.Jira().view("NOPE-1")
    assert "doesn't exist" in (e.value.detail or "")


def test_transition_never_waits_for_a_prompt(shell):
    jira.Jira().transition("PLAT-412", "In Review")
    assert shell.transitions == [("PLAT-412", "In Review")]


def test_transition_failure_points_at_the_workflow(shell):
    shell.transition_error = "✗ Error: no transition to status 'In Review' is available"
    with pytest.raises(ToddError, match="Couldn't move PLAT-412 to In Review") as e:
        jira.Jira().transition("PLAT-412", "In Review")
    assert "workflow" in (e.value.hint or "")


def test_missing_acli_says_how_to_install(shell):
    shell.missing.add("acli")
    with pytest.raises(ToddError) as e:
        jira.Jira().view("PLAT-412")
    assert "acli jira auth login" in (e.value.hint or "")
