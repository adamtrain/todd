import tomllib

import pytest

from todd import config
from todd.errors import ToddError
from todd.models import State


def test_defaults_when_there_is_no_file(tmp_path):
    loaded = config.load(tmp_path / "missing.toml")
    assert not loaded.loaded
    assert loaded.claude.command == "claude"
    assert loaded.claude.effort == "low"
    assert loaded.jira.command == "acli"
    assert loaded.jira.target("PLAT-1", State.DOING) == "In Progress"
    assert loaded.jira.target("PLAT-1", State.IN_REVIEW) == "In Review"
    assert loaded.jira.target("PLAT-1", State.WAITING) is None
    assert loaded.slack.prompt_on == {State.IN_REVIEW, State.DONE}


def test_the_starter_template_parses_to_the_defaults():
    parsed = config.parse(tomllib.loads(config.TEMPLATE))
    assert parsed.jira.status == config.DEFAULT_JIRA_STATUS
    assert parsed.projects == {}


def test_per_project_overrides(config_file):
    path = config_file(
        """
[jira]
site = "acme.atlassian.net"
keys = ["plat"]

[jira.status]
doing = "In Progress"
waiting = "Blocked"
in-review = "Code Review"
done = "Done"

[jira.projects.OPS.status]
in_review = ""
done = "Closed"

[slack]
prompt_on = ["done"]

[projects]
platform = "Infra, CI, migrations"
"""
    )
    loaded = config.load(path)
    jira = loaded.jira
    assert jira.site == "acme.atlassian.net"
    assert jira.known_keys == {"PLAT", "OPS"}
    assert jira.target("PLAT-412", State.IN_REVIEW) == "Code Review"
    assert jira.target("PLAT-412", State.WAITING) == "Blocked"
    assert jira.target("OPS-9", State.IN_REVIEW) is None
    assert jira.target("OPS-9", State.DONE) == "Closed"
    assert jira.target("OPS-9", State.DOING) == "In Progress"
    assert loaded.slack.prompt_on == {State.DONE}
    assert loaded.projects == {"platform": "Infra, CI, migrations"}


def test_listing_status_replaces_the_defaults(config_file):
    loaded = config.load(config_file('[jira.status]\ndone = "Resolved"\n'))
    assert loaded.jira.target("X-1", State.DOING) is None
    assert loaded.jira.target("X-1", State.DONE) == "Resolved"


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("[jira]\nsitee = 'x'\n", "Unknown setting in \\[jira\\]: sitee"),
        ("[jira.status]\nfinished = 'Done'\n", "isn't a todd state"),
        ("[claude]\neffort = 'extreme'\n", "effort must be one of"),
        ("[claude]\ntimeout = true\n", "wrong type"),
        ("[projects]\nplatform = 3\n", "short description"),
        ("[jira\n", "isn't valid TOML"),
    ],
)
def test_mistakes_are_explained(config_file, text, complaint):
    with pytest.raises(ToddError, match=complaint):
        config.load(config_file(text))
