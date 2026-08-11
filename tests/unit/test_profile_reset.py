"""Disconnecting a single integration from a profile.

The dashboard's per-integration "Disconnect" drops one credential without
deleting the whole profile, so the failure to guard against is a reset that
silently keeps the secret (the row still reads connected) or one that takes the
neighbours down with it. These assert the env keys and on-disk artifacts a
reset owns go, and that everything else stays.
"""

from pathlib import Path

import pytest

import inkwell.devtools.setup as setup


def test_clear_env_file_drops_only_named_keys(tmp_path: Path) -> None:
    env = tmp_path / "env"
    setup.write_env_file(env, {"A": "1", "B": "2", "C": "3"})
    setup.clear_env_file(env, ["B"])
    assert setup.read_env_file(env) == {"A": "1", "C": "3"}


def test_clear_env_file_is_noop_when_keys_absent(tmp_path: Path) -> None:
    env = tmp_path / "env"
    setup.write_env_file(env, {"A": "1"})
    before = env.read_text()
    setup.clear_env_file(env, ["MISSING"])
    assert env.read_text() == before


def test_reset_google_clears_keys_and_deletes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "PROFILES_DIR", tmp_path / "profiles")
    profile = "alice"
    google = setup.google_paths_for_profile(profile)
    creds, token = google.credentials, google.token
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    token.write_text("{}")
    setup.write_env_local(
        {
            "GOOGLE_CREDENTIALS_PATH": str(creds),
            "GOOGLE_TOKEN_PATH": str(token),
            "EXA_API_KEY": "exa-123",
        },
        profile,
    )

    setup.reset_integration("google", profile)

    assert not creds.exists()
    assert not token.exists()
    env = setup.read_env_local(profile)
    assert "GOOGLE_CREDENTIALS_PATH" not in env
    assert "GOOGLE_TOKEN_PATH" not in env
    assert env["EXA_API_KEY"] == "exa-123"


def test_reset_research_key_leaves_other_integrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "PROFILES_DIR", tmp_path / "profiles")
    profile = "alice"
    setup.write_env_local(
        {"EXA_API_KEY": "exa-123", "INKWELL_AUTHOR_EMAIL": "alice@example.com"},
        profile,
    )

    setup.reset_integration("exa", profile)

    env = setup.read_env_local(profile)
    assert "EXA_API_KEY" not in env
    assert env["INKWELL_AUTHOR_EMAIL"] == "alice@example.com"


def test_reset_unknown_integration_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup, "PROFILES_DIR", tmp_path / "profiles")
    with pytest.raises(KeyError):
        setup.reset_integration("bogus", "alice")
