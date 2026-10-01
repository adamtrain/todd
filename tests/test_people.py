import pytest

from todd.errors import ToddError
from todd.people import Nicknames


def test_names_by_login_ignoring_case_and_at_signs(tmp_path):
    names = Nicknames(tmp_path / "nicknames.toml")
    names.set("@Priya-N", "Priya")
    assert names.name("priya-n") == "Priya"
    assert names.name("PRIYA-N") == "Priya"
    assert names.name("sam-k") == "sam-k"
    assert names.name(None) is None
    assert names.describe("priya-n") == "Priya (GitHub @priya-n)"
    assert names.describe("sam-k") == "GitHub @sam-k"


def test_saved_and_loaded(tmp_path):
    path = tmp_path / "nicknames.toml"
    Nicknames(path).set("acme/platform-reviewers", 'The "platform" crew')
    again = Nicknames(path)
    assert again.items() == [("acme/platform-reviewers", 'The "platform" crew')]
    again.set("acme/platform-reviewers", None)
    assert Nicknames(path).items() == []


def test_aliases_go_both_ways():
    names = Nicknames(names={"priya-n": "Priya"})
    assert names.aliases("priya-n") == ["priya-n", "Priya"]
    assert names.aliases("priya") == ["priya", "priya-n"]
    assert names.aliases("zed") == ["zed"]


def test_a_broken_file_is_explained(tmp_path):
    path = tmp_path / "nicknames.toml"
    path.write_text("priya-n = ")
    with pytest.raises(ToddError, match="Couldn't read nicknames"):
        Nicknames(path)
