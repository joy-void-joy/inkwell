# claude: ignore
"""Dashboard Google OAuth consent mechanism — client-type routing and the
split-request PKCE handling, scope-agnostic so each agent binds its own scopes.

The dashboard runs OAuth as two separate requests: one builds the consent URL,
a later one exchanges the returned code. Each uses its own ``Flow`` object, so
the PKCE ``code_verifier`` the library generates when the URL is built must be
carried across to the exchange — a fresh exchange flow has none, which Google
rejects as ``invalid_grant: Missing code verifier``. These helpers make that
explicit: :func:`build_consent_url` returns the verifier and
:func:`exchange_code` accepts it.

The scopes are a parameter rather than a module constant so one implementation
serves every caller — the assistant's Gmail/Calendar token and inkwell's
Docs/Drive token alike. :func:`exchange_code` returns the credentials without
persisting; each caller writes its own token file where it keeps it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# A loopback redirect that Google accepts for Desktop OAuth clients. On a headless
# host nothing listens here, but the consent redirect still lands in the browser's
# address bar with ?code=..., which the user copies back into the dashboard.
MANUAL_REDIRECT_URI = "http://localhost"


def client_type(credentials_path: str) -> str:
    """Return ``"web"`` or ``"installed"`` for an OAuth client JSON (``""`` if unreadable).

    The dashboard routes its Google authorization by this: a Web-application client
    can finish consent on the dashboard's own callback URL (any origin), while a
    Desktop (``"installed"``) client can only redirect to loopback — so a remote
    dashboard must fall back to the copy-paste flow with one.
    """
    try:
        data: object = json.loads(Path(credentials_path).read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        if "web" in data:
            return "web"
        if "installed" in data:
            return "installed"
    return ""


def build_consent_url(
    credentials_path: str,
    redirect_uri: str,
    scopes: list[str],
    *,
    state: str | None = None,
    use_pkce: bool = True,
) -> tuple[str, str, str]:
    """Build a Google consent URL, returning ``(url, state, code_verifier)``.

    The redirect target is a parameter so one flow serves both paths: the
    copy-paste fallback points it at a loopback URL with no listener (the dead
    page the user reads the code off of), while the dashboard's auto-callback
    points it at the dashboard's own origin so the redirect lands on a real route
    that exchanges the code — no page to copy from. The returned ``state`` ties
    the eventual callback back to the flow that started it.

    When ``use_pkce`` is set, the URL carries a PKCE ``code_challenge`` and the
    matching ``code_verifier`` is returned for :func:`exchange_code` to present.
    The copy-paste flow disables it: a hand-pasted bare code carries no ``state``
    to recover the verifier by, so it falls back to the classic non-PKCE exchange
    (returning ``""``).
    """
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {creds_path}")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_path), scopes, autogenerate_code_verifier=use_pkce
    )
    flow.redirect_uri = redirect_uri
    auth_url, returned_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return auth_url, returned_state, flow.code_verifier or ""


def exchange_code(
    credentials_path: str,
    redirect_uri: str,
    code: str,
    scopes: list[str],
    *,
    code_verifier: str | None = None,
) -> Credentials:
    """Exchange an authorization code for OAuth credentials, for ``redirect_uri``.

    ``redirect_uri`` must match the one :func:`build_consent_url` used, and
    ``code_verifier`` must be the one it returned whenever the consent URL carried
    a PKCE challenge — otherwise Google rejects the exchange with
    ``invalid_grant: Missing code verifier``. Google may grant extra scopes (e.g.
    ``openid``), so token-scope relaxation is enabled to avoid a spurious mismatch
    error on exchange. The credentials are returned unpersisted; the caller writes
    its own token file.
    """
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {creds_path}")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
    flow.redirect_uri = redirect_uri
    flow.code_verifier = code_verifier or None
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not isinstance(creds, Credentials):
        raise RuntimeError("OAuth flow returned unexpected credential type")
    return creds
