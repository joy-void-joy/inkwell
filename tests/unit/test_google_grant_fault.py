"""A grant Google has stopped honoring has to read as broken, not as present.

Every part of a work is written into a document, so a revoked token is not one
part's problem: it is the reason none of them can be written. The token file
survives the revocation untouched, which is why finding it proves nothing —
inkwell's status said "authorized" for exactly the token its runs were dying
on, and the loop rediscovered the same fault once per part, each having
already paid for a plan it would never write.
"""

from pathlib import Path

import pytest

import inkwell.agent.google_auth as google_auth
from inkwell.agent.config import Settings, use_settings
from inkwell.devtools.setup import GooglePaths, google_standing

REVOKED = "Failed to refresh Google token: invalid_grant. Run `inkwell setup`."


def refusing(_token_path: str) -> google_auth.Credentials:
    raise google_auth.GoogleAuthError(REVOKED)


def test_a_revoked_grant_is_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_auth, "load_credentials", refusing)
    assert google_auth.grant_fault("/tmp/token.json") == REVOKED


def test_a_grant_that_refreshes_is_no_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_auth, "load_credentials", lambda path: path)
    assert google_auth.grant_fault("/tmp/token.json") is None


def test_an_unreachable_google_is_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused rather than passed: a run would fail on the same call."""

    def offline(_token_path: str) -> google_auth.Credentials:
        raise google_auth.TransportError("connection refused")

    monkeypatch.setattr(google_auth, "load_credentials", offline)
    assert google_auth.grant_fault("/tmp/token.json") == "connection refused"


def test_a_profile_naming_no_token_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    unconfigured = Settings.model_validate({})
    unconfigured.google_token_path = None
    with use_settings(unconfigured):
        fault = google_auth.document_fault()
    assert fault is not None
    assert "GOOGLE_TOKEN_PATH is not set" in fault


def test_the_settings_token_is_the_one_asked_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        google_auth, "grant_fault", lambda path: asked.append(path) or None
    )
    scoped = Settings.model_validate({})
    scoped.google_token_path = "/tmp/cesia/token.json"
    with use_settings(scoped):
        assert google_auth.document_fault() is None
    assert asked == ["/tmp/cesia/token.json"]


def test_status_reports_a_revoked_token_as_unauthorized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The row a `setup status` reader trusts, over the token that outlived it."""
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(google_auth, "load_credentials", refusing)

    standing = google_standing(
        GooglePaths(credentials=tmp_path / "google.json", token=token)
    )

    assert standing.authorized is False
    assert standing.detail == REVOKED


def test_status_reports_a_working_token_as_authorized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(google_auth, "load_credentials", lambda path: path)

    standing = google_standing(
        GooglePaths(credentials=tmp_path / "google.json", token=token)
    )

    assert standing.authorized is True
    assert standing.detail == "authorized"


def test_status_keeps_the_two_ways_a_token_can_be_absent(tmp_path: Path) -> None:
    creds = tmp_path / "google.json"
    absent = google_standing(
        GooglePaths(credentials=creds, token=tmp_path / "token.json")
    )
    assert absent.detail == "not configured"

    creds.write_text("{}")
    unauthorized = google_standing(
        GooglePaths(credentials=creds, token=tmp_path / "token.json")
    )
    assert unauthorized.detail == "credentials present, not yet authorized"
