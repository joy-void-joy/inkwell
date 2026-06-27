"""Inkwell's dashboard Google web-callback OAuth flow.

Mirrors the assistant's: a consent URL is built in one request and the code is
exchanged in a later one, so the PKCE ``code_verifier`` must survive across the
split — carried in ``PENDING_GOOGLE_AUTH`` by ``state`` — or Google rejects the
exchange with ``invalid_grant: Missing code verifier``. The copy-paste fallback
turns PKCE off, and the redirect target follows the OAuth client type: a Web
client reuses the dashboard callback, a Desktop client the dead loopback page.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import inkwell.agent.google_auth as google_auth
from inkwell.environment.web.routes import profiles as profiles_route


def write_client(path: Path, kind: str) -> Path:
    path.write_text(json.dumps({kind: {"client_id": "x", "client_secret": "y"}}))
    return path


def test_validated_callback_rejects_non_http() -> None:
    with pytest.raises(HTTPException):
        profiles_route.validated_callback("not-a-url")


def test_validated_callback_trims_trailing_slash() -> None:
    trimmed = profiles_route.validated_callback(
        "http://host:8765/inkwell/api/google/callback/"
    )
    assert trimmed == "http://host:8765/inkwell/api/google/callback"


def test_manual_redirect_web_uses_dashboard_callback(tmp_path: Path) -> None:
    creds = write_client(tmp_path / "c.json", "web")
    callback = "http://host:8765/inkwell/api/google/callback"
    assert profiles_route.manual_redirect_for(str(creds), callback) == callback


def test_manual_redirect_desktop_uses_loopback(tmp_path: Path) -> None:
    creds = write_client(tmp_path / "c.json", "installed")
    redirect = profiles_route.manual_redirect_for(
        str(creds), "http://host/inkwell/api/google/callback"
    )
    assert redirect == profiles_route.MANUAL_REDIRECT_URI


def test_callback_round_trip_threads_pkce_verifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The web-callback flow builds with PKCE, stores the verifier by state, and the
    # callback redeems the code against that same verifier — the exact thread that
    # was missing. Asserts the verifier reaches the exchange and the token is saved.
    creds = write_client(tmp_path / "c.json", "web")
    token = tmp_path / "token.json"
    monkeypatch.setattr(
        profiles_route, "google_paths_for_profile", lambda _name: (creds, token)
    )
    monkeypatch.setattr(
        google_auth,
        "build_consent_url",
        lambda _c, _r, *, state=None, use_pkce=True: (
            "https://accounts.google.com/o/oauth2/auth",
            "S1",
            "VERIFIER" if use_pkce else "",
        ),
    )
    exchanged: dict[str, str] = {}
    monkeypatch.setattr(
        google_auth,
        "exchange_code",
        lambda _c, redirect, code, _t, *, code_verifier=None: exchanged.update(
            redirect=redirect, code=code, verifier=code_verifier or ""
        ),
    )
    written: dict[str, str] = {}
    monkeypatch.setattr(
        profiles_route,
        "write_env_local",
        lambda values, profile: written.update(values),
    )

    callback = "http://host:8765/inkwell/api/google/callback"
    asyncio.run(profiles_route.start_consent("alice", callback, use_pkce=True))
    assert "S1" in profiles_route.PENDING_GOOGLE_AUTH

    page = asyncio.run(profiles_route.google_callback(state="S1", code="AUTH"))
    assert page.status_code == 200
    assert exchanged["redirect"] == callback
    assert exchanged["code"] == "AUTH"
    assert exchanged["verifier"] == "VERIFIER"
    assert written["GOOGLE_TOKEN_PATH"] == str(token)
    assert "S1" not in profiles_route.PENDING_GOOGLE_AUTH


def test_callback_unknown_state_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An expired/forged state degrades to an error page, never an exchange or write.
    calls: list[object] = []
    monkeypatch.setattr(
        profiles_route, "write_env_local", lambda *a, **_k: calls.append(a)
    )
    page = asyncio.run(profiles_route.google_callback(state="ghost", code="x"))
    assert page.status_code == 200
    assert "expired" in page.body.decode().lower()
    assert calls == []
