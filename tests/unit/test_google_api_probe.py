"""Inkwell probes whether the Docs/Drive APIs are enabled, not just whether a
token exists: a fully authorized token still 403s Doc creation with
SERVICE_DISABLED until the APIs are switched on for the project, and
re-authorizing never fixes that — so the status reflects enablement directly.
"""

import asyncio
import json
from pathlib import Path

import pytest

import inkwell.agent.google_auth as google_auth
from inkwell.environment.web.routes import profiles as profiles_route


def test_service_disabled_body_detected() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "SERVICE_DISABLED",
                        "domain": "googleapis.com",
                    }
                ],
            }
        }
    )
    assert google_auth.is_service_disabled_body(body) is True
    assert google_auth.is_service_disabled_body(body.encode()) is True


def test_not_found_is_not_service_disabled() -> None:
    # A reachable API returning 404 for the bogus probe id means it is enabled.
    body = json.dumps({"error": {"code": 404, "status": "NOT_FOUND", "details": []}})
    assert google_auth.is_service_disabled_body(body) is False


def test_malformed_body_is_not_service_disabled() -> None:
    assert google_auth.is_service_disabled_body("{not json") is False


def test_status_reports_disabled_apis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(
        profiles_route, "google_paths_for_profile", lambda _n: (creds, token)
    )
    monkeypatch.setattr(
        profiles_route, "probe_disabled_apis", lambda _t: ["docs.googleapis.com"]
    )
    status = asyncio.run(profiles_route.google_status("alice"))
    assert status.disabled_apis == ["docs.googleapis.com"]


def test_status_skips_probe_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No token means nothing to probe — the network call must not fire.
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    monkeypatch.setattr(
        profiles_route, "google_paths_for_profile", lambda _n: (creds, token)
    )
    called: list[str] = []
    monkeypatch.setattr(
        profiles_route, "probe_disabled_apis", lambda t: called.append(t)
    )
    status = asyncio.run(profiles_route.google_status("alice"))
    assert status.has_token is False
    assert status.disabled_apis is None
    assert called == []
