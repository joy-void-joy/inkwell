"""One profile name, meaning one account everywhere it is spelled.

The failure to guard against is a split: a name the writing agent resolves to
one directory while a native launch resolves it to another, or to nothing.
These pin that the harness selects from the same ``profiles/`` directory the
wizard fills in, that a recorded selection is what both read when no name is
given, and that the two curations an inkwell profile cannot express say so
rather than doing something surprising.
"""

from pathlib import Path

import pytest

import inkwell.agent.config as config
import inkwell.devtools.setup as setup
from inkwell.devtools.profiles import inkwell_profile_directory


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A project whose profiles and recorded selection live under tmp_path."""
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(setup, "PROFILES_DIR", profiles)
    monkeypatch.setattr(config, "ACTIVE_PROFILE_FILE", profiles / ".active")
    monkeypatch.setattr(config.override, "name", None)
    monkeypatch.delenv("INKWELL_PROFILE", raising=False)
    return profiles


def test_a_launch_selects_the_home_the_wizard_fills_in(project: Path) -> None:
    setup.write_env_local({"EXA_API_KEY": "exa-123"}, "alice")

    directory = inkwell_profile_directory()

    assert [entry.name for entry in directory.entries()] == ["alice"]
    assert directory.launch_home("alice") == setup.claude_config_dir_for_profile(
        "alice"
    )
    assert directory.launch_home("alice") == project / "alice" / "claude-config"


def test_naming_no_profile_selects_the_recorded_one(project: Path) -> None:
    setup.write_env_local({"EXA_API_KEY": "exa-123"}, "alice")
    directory = inkwell_profile_directory()
    assert directory.launch_home(None) is None

    directory.use("alice")

    assert config.active_profile() == "alice"
    assert directory.launch_home(None) == project / "alice" / "claude-config"


def test_the_environment_still_wins_over_the_recorded_selection(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.write_env_local({"EXA_API_KEY": "a"}, "alice")
    setup.write_env_local({"EXA_API_KEY": "b"}, "bob")
    inkwell_profile_directory().use("alice")

    monkeypatch.setenv("INKWELL_PROFILE", "bob")

    assert config.active_profile() == "bob"
    assert inkwell_profile_directory().launch_home(None) == (
        project / "bob" / "claude-config"
    )


def test_an_unknown_profile_is_a_loud_error(project: Path) -> None:
    """The directory formats the refusal, so every caller reads one wording.

    Still a KeyError, which is what this origin raises, so the store did not
    have to learn a new type to be reported well.
    """
    with pytest.raises(KeyError, match="unknown profile 'ghost'"):
        inkwell_profile_directory().launch_home("ghost")


def test_adding_a_profile_starts_the_directory_the_wizard_fills_in(
    project: Path,
) -> None:
    added = inkwell_profile_directory().add("alice")

    assert added.config_dir == project / "alice" / "claude-config"
    assert setup.env_file_for_profile("alice").exists()
    assert setup.list_profiles() == ["alice"]


def test_a_home_outside_the_profile_directory_is_refused(project: Path) -> None:
    with pytest.raises(ValueError, match="derived from the name"):
        inkwell_profile_directory().add("alice", Path("/somewhere/else"))


def test_removing_a_profile_refuses_rather_than_deleting_its_credentials(
    project: Path,
) -> None:
    setup.write_env_local({"EXA_API_KEY": "exa-123"}, "alice")

    with pytest.raises(ValueError, match="remove that directory"):
        inkwell_profile_directory().remove("alice")

    assert setup.list_profiles() == ["alice"]
