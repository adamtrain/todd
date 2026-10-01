from datetime import UTC, datetime

import pytest

from todd import links
from todd.models import Link, LinkKind

from .conftest import PR_URL, SLACK_DM, SLACK_THREAD


def test_slack_permalink_is_understood_without_asking_slack():
    message = links.slack_message(SLACK_DM)
    assert message is not None
    assert message.workspace == "acme"
    assert message.channel == "D024BE91L"
    assert message.ts == "1790776800.123456"
    assert message.where == "DM"
    assert message.posted == datetime(2026, 9, 30, 14, 0, 0, 123456, tzinfo=UTC)
    assert not message.in_thread


def test_slack_reply_in_a_thread():
    message = links.slack_message(SLACK_THREAD)
    assert message is not None
    assert message.where == "channel"
    assert message.thread_ts == "1790674200.000100"
    assert message.in_thread
    assert message.ref == "C01ABCDEF/1790780400.000200"


def test_enterprise_grid_slack_links():
    url = "https://acme-corp.enterprise.slack.com/archives/G0PRIVATE/p1790776800123456"
    message = links.slack_message(url)
    assert message is not None
    assert message.workspace == "acme-corp"
    assert message.where == "private channel or group DM"


def test_a_slack_link_that_isnt_a_message_is_still_slack():
    link = links.from_url("https://acme.slack.com/client/T123/C456")
    assert link.kind == LinkKind.SLACK
    assert link.ref is None


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://acme.atlassian.net/browse/PLAT-412", "PLAT-412"),
        ("https://acme.atlassian.net/browse/PLAT-412?focusedCommentId=1", "PLAT-412"),
        (
            "https://acme.atlassian.net/jira/software/projects/PLAT/boards/7?selectedIssue=PLAT-9",
            "PLAT-9",
        ),
        ("https://jira.internal.example.com/browse/OPS_2-15", "OPS_2-15"),
    ],
)
def test_jira_urls(url, key):
    link = links.from_url(url)
    assert link.kind == LinkKind.JIRA
    assert link.ref == key


def test_bare_jira_key_argument_uses_the_configured_site():
    link = links.recognize("PLAT-412", site="acme.atlassian.net")
    assert link == Link(LinkKind.JIRA, "https://acme.atlassian.net/browse/PLAT-412", ref="PLAT-412")
    bare = links.recognize("PLAT-412")
    assert bare is not None and bare.url is None


def test_github_pull_request():
    link = links.from_url(PR_URL)
    assert link.kind == LinkKind.GITHUB
    assert link.ref == "acme/billing#86"


def test_anything_else_is_a_plain_link():
    link = links.from_url("https://docs.google.com/document/d/abc/edit")
    assert link.kind == LinkKind.URL


def test_words_are_not_links():
    assert links.recognize("hello") is None
    assert links.recognize("utf-8") is None


def test_urls_inside_text_lose_trailing_punctuation():
    found = links.in_text(
        "see (https://example.com/a) and https://en.wikipedia.org/wiki/Foo_(bar), then "
        f"<{SLACK_DM}>."
    )
    assert [link.url for link in found] == [
        "https://example.com/a",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        SLACK_DM,
    ]


def test_jira_keys_in_text_only_count_for_known_projects():
    text = "Fix PLAT-412 before OPS-9; also UTF-8 and SHA-256 aren't tickets"
    assert links.in_text(text) == []
    found = links.in_text(text, keys={"PLAT", "OPS"}, site="acme.atlassian.net")
    assert [link.ref for link in found] == ["PLAT-412", "OPS-9"]


def test_jira_key_inside_a_url_is_not_counted_twice():
    found = links.in_text("https://acme.atlassian.net/browse/PLAT-412", keys={"PLAT"})
    assert len(found) == 1


def test_dedupe_keeps_first_and_any_quote():
    a = Link(LinkKind.SLACK, SLACK_DM, ref="D024BE91L/1790776800.123456")
    b = Link(LinkKind.SLACK, SLACK_DM, ref="D024BE91L/1790776800.123456", quote="hi")
    kept = links.dedupe([a, b])
    assert kept == [a]
    assert a.quote == "hi"


def test_labels():
    assert links.label(links.from_url(PR_URL)) == "acme/billing#86"
    assert links.label(links.from_url(SLACK_DM)).startswith("Slack DM · Sep 30")
    assert links.label(links.from_url(SLACK_THREAD)).startswith("Slack channel thread")
    assert links.label(links.from_url("https://www.example.com/a/b/")) == "example.com/a/b"
