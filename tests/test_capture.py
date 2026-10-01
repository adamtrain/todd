import pytest

from todd import capture
from todd.errors import ToddError
from todd.models import LinkKind

from .conftest import PR_URL, SLACK_DM, SLACK_THREAD

SITE = "acme.atlassian.net"


def test_arguments_become_links_and_stay_in_the_description():
    got = capture.from_args(
        ["reply to Priya about the Q3 numbers", SLACK_DM, "PLAT-412"], site=SITE
    )
    # Kept as typed, so "see also <link>" still says what a link is for.
    assert got.description == f"reply to Priya about the Q3 numbers {SLACK_DM} PLAT-412"
    assert [link.kind for link in got.links] == [LinkKind.SLACK, LinkKind.JIRA]
    assert got.links[1].url == "https://acme.atlassian.net/browse/PLAT-412"


def test_unquoted_words_are_joined():
    got = capture.from_args(["review", "the", "PR", PR_URL])
    assert got.description == f"review the PR {PR_URL}"
    assert got.links[0].ref == "acme/billing#86"


def test_links_inside_the_description_are_found_too():
    got = capture.from_args([f"follow up on PLAT-412, see {PR_URL}"], keys={"PLAT"})
    assert got.description == f"follow up on PLAT-412, see {PR_URL}"
    assert [link.ref for link in got.links] == ["PLAT-412", "acme/billing#86"]


def test_quotes_attach_to_slack_links_in_order():
    got = capture.from_args(
        ["two asks", SLACK_DM, "PLAT-412", SLACK_THREAD],
        ["first message", "second message"],
    )
    slack = [link for link in got.links if link.kind == LinkKind.SLACK]
    assert [link.quote for link in slack] == ["first message", "second message"]
    assert got.links[1].quote is None


def test_quote_goes_to_any_link_when_there_is_no_slack_link():
    got = capture.from_args(["see comment", "PLAT-412"], ["the comment text"])
    assert got.links[0].quote == "the comment text"


def test_too_many_quotes_is_an_error():
    with pytest.raises(ToddError, match="2 quotes but only 1 Slack link"):
        capture.from_args(["x", SLACK_DM], ["a", "b"])


def test_text_format_keeps_each_message_with_its_link():
    text = f"""Reply to Priya with the Q3 numbers
before Thursday

{SLACK_DM}
Hey, can you send me the Q3 migration numbers before Thursday's sync?

Need them for the exec deck.

PLAT-412

{SLACK_THREAD}
> Sam: +1, I need them too
"""
    got = capture.parse_text(text, site=SITE)
    assert got.description == "Reply to Priya with the Q3 numbers\nbefore Thursday"
    dm, ticket, thread = got.links
    assert dm.quote == (
        "Hey, can you send me the Q3 migration numbers before Thursday's sync?\n\n"
        "Need them for the exec deck."
    )
    assert ticket.ref == "PLAT-412"
    assert ticket.quote is None
    assert thread.quote == "Sam: +1, I need them too"


def test_text_format_accepts_bulleted_links_and_ignores_comments_from_the_editor():
    text = f"# a comment\nDo the thing\n- {PR_URL}\n# another\n"
    got = capture.parse_text(text, comments=True)
    assert got.description == "Do the thing"
    assert got.links[0].ref == "acme/billing#86"


def test_a_line_with_a_link_and_words_is_description_not_a_link_line():
    got = capture.parse_text(f"Look at {PR_URL} today")
    assert got.description == f"Look at {PR_URL} today"
    assert got.links[0].quote is None


def test_editor_buffer_round_trips():
    original = capture.from_args(["reply to Priya", SLACK_DM, "PLAT-412"], ["hello?"], site=SITE)
    again = capture.parse_text(capture.to_text(original), site=SITE, comments=True)
    assert again.description == f"reply to Priya {SLACK_DM} PLAT-412"
    assert [(link.url, link.quote) for link in again.links] == [
        (SLACK_DM, "hello?"),
        ("https://acme.atlassian.net/browse/PLAT-412", None),
    ]


def test_empty_capture():
    assert capture.parse_text("# only comments\n\n", comments=True).empty
