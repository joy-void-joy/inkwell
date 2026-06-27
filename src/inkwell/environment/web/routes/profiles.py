"""REST endpoints for profile management."""

import asyncio
import json
import shutil
from html import escape
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from inkwell.agent.browser_auth import context_has_cookies, login_interactive
from inkwell.agent.google_auth import probe_disabled_apis
from inkwell.devtools.setup import (
    PROFILES_DIR,
    claude_config_dir_for_profile,
    credentials_dir_for_profile,
    env_file_for_profile,
    google_paths_for_profile,
    list_profiles,
    mask,
    read_env_local,
    reset_integration,
    write_env_local,
)
from lup.google_oauth import MANUAL_REDIRECT_URI, client_type

router = APIRouter(prefix="/api/profiles", tags=["profiles"])

# Google's consent redirect lands here, profile-agnostic. The proxying dashboard
# exposes it as ``/inkwell/api/google/callback``; a flow is recovered by ``state``.
callback_router = APIRouter(prefix="/api/google", tags=["google"])


class ServerCapabilities(BaseModel):
    has_display: bool = Field(description="Whether server has a graphical display")
    is_local: bool = Field(description="Whether client appears to be on localhost")


class IntegrationStatus(BaseModel):
    id: str = Field(description="Stable id the dashboard sends to reset this one")
    name: str = Field(description="Integration name")
    configured: bool = Field(description="Whether this integration is configured")
    detail: str = Field(default="", description="Status detail (masked key, etc.)")


class ProfileResponse(BaseModel):
    name: str = Field(description="Profile name")
    integrations: list[IntegrationStatus] = Field(description="Integration statuses")


class UpdateProfileRequest(BaseModel):
    values: dict[str, str] = Field(
        description="Environment variable key-value pairs to set"
    )


class RenameProfileRequest(BaseModel):
    new_name: str = Field(description="New profile name")


class GoogleStatusResponse(BaseModel):
    has_credentials: bool = Field(description="Whether OAuth client JSON exists")
    has_token: bool = Field(description="Whether authorized token exists")
    detail: str = Field(description="Human-readable status")
    client_type: str = Field(
        default="", description="OAuth client kind: 'web', 'installed', or ''"
    )
    disabled_apis: list[str] | None = Field(
        default=None,
        description="Docs/Drive APIs switched off on the client's Cloud project: "
        "empty when all enabled, populated with hostnames when off, None if unprobed",
    )


class GoogleAuthUrlResponse(BaseModel):
    auth_url: str = Field(description="Consent URL to open in the viewer's browser")


class GoogleCallbackStartRequest(BaseModel):
    callback_url: str = Field(
        description="Full dashboard callback URL the browser is redirected back to, "
        "e.g. http://host:8765/inkwell/api/google/callback"
    )


class GoogleAuthCompleteRequest(BaseModel):
    code: str = Field(description="Authorization code (or redirect URL) pasted back")
    callback_url: str = Field(
        default="",
        description="The dashboard callback URL the consent used, echoed back so the "
        "code is redeemed against the same redirect (Web client only)",
    )


class DetectedLogin(BaseModel):
    path: str = Field(description="Absolute path to the config directory")
    label: str = Field(description="Human-readable label for this location")
    is_profile_match: bool = Field(
        description="Whether this matches the current profile"
    )


class DetectResult(BaseModel):
    found: list[DetectedLogin] = Field(
        description="Config directories with credentials"
    )


SETTABLE_KEYS = frozenset(
    {
        "EXA_API_KEY",
        "FRED_API_KEY",
        "CLAUDE_COOKIE",
        "CLAUDE_ORG_UUID",
        "CLAUDE_CONFIG_DIR",
        "INKWELL_AUTHOR_EMAIL",
        "INKWELL_GOOGLE_WORKSPACE_DOMAIN",
        "AGENT_MODEL",
        "AGENT_MAX_BUDGET_USD",
        "OPENROUTER_API_KEY",
    }
)


@router.get("/capabilities")
async def get_capabilities(request: Request) -> ServerCapabilities:
    """Report server-side capabilities so the frontend can adapt its UI."""
    from inkwell.devtools.setup import has_graphical_display

    client_host = request.client.host if request.client else "unknown"
    is_local = client_host in ("127.0.0.1", "::1", "localhost")
    return ServerCapabilities(
        has_display=has_graphical_display(),
        is_local=is_local,
    )


async def build_profile_response(name: str) -> ProfileResponse:
    """Build integration status for a named profile."""
    from pathlib import Path

    env = read_env_local(name)
    _, google_token = google_paths_for_profile(name)

    integrations: list[IntegrationStatus] = []

    config_dir = env.get("CLAUDE_CONFIG_DIR", "")
    login_ok = bool(config_dir) and (Path(config_dir) / ".credentials.json").exists()
    integrations.append(
        IntegrationStatus(
            id="claude-login",
            name="Claude login",
            configured=login_ok,
            detail=config_dir if login_ok else "not configured",
        )
    )

    google_creds, _ = google_paths_for_profile(name)
    google_ok = google_token.exists()
    if google_ok:
        google_detail = "authorized"
    elif google_creds.exists():
        google_detail = "credentials uploaded, not yet authorized"
    else:
        google_detail = "not configured"
    integrations.append(
        IntegrationStatus(
            id="google",
            name="Google",
            configured=google_ok,
            detail=google_detail,
        )
    )

    author_email = env.get("INKWELL_AUTHOR_EMAIL", "")
    integrations.append(
        IntegrationStatus(
            id="author",
            name="Author email",
            configured=bool(author_email),
            detail=author_email or "not configured",
        )
    )

    exa_key = env.get("EXA_API_KEY", "")
    integrations.append(
        IntegrationStatus(
            id="exa",
            name="Exa",
            configured=bool(exa_key),
            detail=mask(exa_key) if exa_key else "not configured",
        )
    )

    has_browser = await context_has_cookies(name)
    claude_cookie = env.get("CLAUDE_COOKIE", "")
    claude_ok = has_browser or bool(claude_cookie)
    if has_browser:
        claude_detail = "browser session stored"
    elif claude_cookie:
        claude_detail = mask(claude_cookie)
    else:
        claude_detail = "not configured"
    integrations.append(
        IntegrationStatus(
            id="session",
            name="Claude.ai session",
            configured=claude_ok,
            detail=claude_detail,
        )
    )

    fred_key = env.get("FRED_API_KEY", "")
    integrations.append(
        IntegrationStatus(
            id="fred",
            name="FRED",
            configured=bool(fred_key),
            detail=mask(fred_key) if fred_key else "not configured",
        )
    )

    return ProfileResponse(name=name, integrations=integrations)


@router.get("")
async def get_profiles() -> list[ProfileResponse]:
    return [await build_profile_response(name) for name in list_profiles()]


@router.get("/{name}")
async def get_profile(name: str) -> ProfileResponse:
    env_path = env_file_for_profile(name)
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return await build_profile_response(name)


@router.put("/{name}")
async def update_profile(name: str, req: UpdateProfileRequest) -> ProfileResponse:
    disallowed = set(req.values.keys()) - SETTABLE_KEYS
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Keys not allowed via API: {', '.join(sorted(disallowed))}",
        )
    write_env_local(req.values, profile=name)
    return await build_profile_response(name)


@router.post("/{name}", status_code=201)
async def create_profile(name: str) -> ProfileResponse:
    env_path = env_file_for_profile(name)
    if env_path.exists():
        raise HTTPException(status_code=409, detail=f"Profile '{name}' already exists")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("# Auto-generated by Inkwell\n")
    credentials_dir_for_profile(name).mkdir(parents=True, exist_ok=True)
    return await build_profile_response(name)


@router.patch("/{name}")
async def rename_profile(name: str, req: RenameProfileRequest) -> ProfileResponse:
    profile_dir = PROFILES_DIR / name
    if not profile_dir.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Profile name cannot be empty")
    new_dir = PROFILES_DIR / new_name
    if new_dir.exists():
        raise HTTPException(
            status_code=409, detail=f"Profile '{new_name}' already exists"
        )
    profile_dir.rename(new_dir)
    return await build_profile_response(new_name)


@router.delete("/{name}", status_code=204)
async def delete_profile(name: str) -> None:
    profile_dir = PROFILES_DIR / name
    if not profile_dir.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    shutil.rmtree(profile_dir)


@router.post("/{name}/reset/{integration}")
async def reset_profile_integration(name: str, integration: str) -> ProfileResponse:
    """Disconnect one integration for a profile, leaving the rest intact.

    Drops just that integration's env keys and on-disk artifacts (the Google
    token, the claude.ai session), so a credential can be re-authorized without
    deleting and rebuilding the whole profile.
    """
    env_path = env_file_for_profile(name)
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    try:
        await asyncio.to_thread(reset_integration, integration, name)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown integration '{integration}'"
        ) from exc
    return await build_profile_response(name)


@router.post("/{name}/login")
async def trigger_login(name: str) -> ProfileResponse:
    """Open a managed browser for the user to log into claude.ai."""
    from inkwell.devtools.setup import has_graphical_display

    if not has_graphical_display():
        raise HTTPException(
            status_code=400,
            detail=(
                "No graphical display available on the server. "
                "Paste a CLAUDE_COOKIE value manually instead."
            ),
        )
    env_path = env_file_for_profile(name)
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    logged_in = await login_interactive(name)
    if not logged_in:
        raise HTTPException(
            status_code=400, detail="Login did not complete successfully"
        )
    return await build_profile_response(name)


@router.post("/{name}/claude-login/detect")
async def detect_claude_login(name: str) -> DetectResult:
    """Scan known locations for Claude credentials. Does not auto-save."""
    from pathlib import Path

    env_path = env_file_for_profile(name)
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    profile_dir = claude_config_dir_for_profile(name)
    default_dir = claude_config_dir_for_profile(None)

    candidates: list[tuple[Path, str, bool]] = [
        (profile_dir, f'Profile "{name}"', True),
        (default_dir, "Default (shared across profiles)", False),
        (Path.home() / ".claude", "Claude Code login (your dev account)", False),
    ]

    found = [
        DetectedLogin(path=str(d), label=label, is_profile_match=match)
        for d, label, match in candidates
        if d.is_dir() and (d / ".credentials.json").exists()
    ]

    return DetectResult(found=found)


def build_google_status(profile: str) -> GoogleStatusResponse:
    """Check Google credentials and token file status for a profile."""
    creds_path, token_path = google_paths_for_profile(profile)
    has_creds = creds_path.exists()
    has_token = token_path.exists()
    if has_token:
        detail = "authorized"
    elif has_creds:
        detail = "credentials uploaded — click Authorize to complete setup"
    else:
        detail = "not configured"
    return GoogleStatusResponse(
        has_credentials=has_creds,
        has_token=has_token,
        detail=detail,
        client_type=client_type(str(creds_path)) if has_creds else "",
    )


@router.get("/{name}/google/status")
async def google_status(name: str) -> GoogleStatusResponse:
    """Check Google OAuth status for a profile.

    Beyond the token-file check, this probes whether the Docs/Drive APIs are
    actually enabled on the client's project — a token can exist yet Doc creation
    still 403 with SERVICE_DISABLED, which re-authorizing never fixes.
    """
    status = build_google_status(name)
    if status.has_token:
        _, token_path = google_paths_for_profile(name)
        status.disabled_apis = await asyncio.to_thread(
            probe_disabled_apis, str(token_path)
        )
    return status


@router.post("/{name}/google/upload-credentials")
async def upload_google_credentials(
    name: str,
    file: UploadFile,
) -> GoogleStatusResponse:
    """Upload the Google OAuth client credentials JSON file."""
    content = await file.read()
    try:
        json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON file: {exc}",
        ) from exc

    creds_path, _ = google_paths_for_profile(name)
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_bytes(content)
    return build_google_status(name)


@router.post("/{name}/google/authorize")
async def authorize_google(name: str) -> GoogleStatusResponse:
    """Run the Google OAuth browser flow and save the token."""
    from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

    from inkwell.agent.google_auth import run_oauth_flow

    creds_path, token_path = google_paths_for_profile(name)

    if not creds_path.exists():
        raise HTTPException(
            status_code=400,
            detail="Upload OAuth client credentials JSON first.",
        )

    try:
        await asyncio.to_thread(run_oauth_flow, str(creds_path), str(token_path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(
            status_code=403,
            detail=(
                "Access denied — the account you signed in with is not listed "
                "as a test user on the project (this can differ from the project "
                "owner). Go to Google Cloud Console → OAuth consent → Audience, "
                "click '+ Add users', and enter that account's email."
            ),
        ) from exc
    except RuntimeError as exc:
        error_msg = str(exc).lower()
        if "access_denied" in error_msg or "verification" in error_msg:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Access denied — make sure the account you're authorizing "
                    "with is listed as a test user in the Google Cloud Console "
                    "OAuth consent screen (can differ from the project owner)."
                ),
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    write_env_local(
        {
            "GOOGLE_CREDENTIALS_PATH": str(creds_path),
            "GOOGLE_TOKEN_PATH": str(token_path),
        },
        profile=name,
    )
    return build_google_status(name)


def extract_code(pasted: str) -> str:
    """Accept either a bare authorization code or the full redirect URL."""
    text = pasted.strip()
    if "code=" in text:
        return parse_qs(urlparse(text).query).get("code", [text])[0]
    return text


def validated_callback(callback_url: str) -> str:
    """Return ``callback_url`` trimmed, rejecting a non-HTTP(S) value.

    Only the dashboard knows its own externally reachable callback — it sits behind
    the dashboard's ``/inkwell`` proxy, a prefix this server is unaware of — so the
    frontend supplies the full URL and this guards it before it reaches Google.
    """
    trimmed = callback_url.rstrip("/")
    parsed = urlparse(trimmed)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid dashboard callback URL")
    return trimmed


def manual_redirect_for(credentials_path: str, callback_url: str) -> str:
    """Pick the copy-paste redirect target for this OAuth client's type.

    A Web client only honors redirect URIs it has registered, so it reuses the very
    dashboard callback the auto flow registers; a Desktop client accepts any loopback
    redirect, so it keeps the dead ``localhost`` page the code is read off of (and
    its irrelevant ``callback_url`` goes unvalidated).
    """
    if client_type(credentials_path) == "web":
        return validated_callback(callback_url)
    return MANUAL_REDIRECT_URI


class PendingGoogleAuth(BaseModel):
    """A consent flow in flight, recovered by ``state`` when Google redirects back."""

    credentials_path: str
    redirect_uri: str
    token_path: str
    profile: str
    code_verifier: str = ""


# Keyed by the OAuth ``state``; consumed once on callback. In-process is enough —
# inkwell is a single server, and an abandoned flow is just a stale entry.
PENDING_GOOGLE_AUTH: dict[str, PendingGoogleAuth] = {}


async def start_consent(name: str, redirect_uri: str, *, use_pkce: bool) -> str:
    """Build a consent URL for ``redirect_uri`` and register its ``state``.

    Shared by the auto-callback and copy-paste builders, so the eventual redirect —
    caught by the dashboard or pasted back — ties to this flow and exchanges against
    the same ``redirect_uri`` and PKCE ``code_verifier``.
    """
    from inkwell.agent.google_auth import build_consent_url

    creds_path, token_path = google_paths_for_profile(name)
    if not creds_path.exists():
        raise HTTPException(
            status_code=400, detail="Upload OAuth client credentials JSON first."
        )
    auth_url, state, verifier = await asyncio.to_thread(
        build_consent_url, str(creds_path), redirect_uri, use_pkce=use_pkce
    )
    PENDING_GOOGLE_AUTH[state] = PendingGoogleAuth(
        credentials_path=str(creds_path),
        redirect_uri=redirect_uri,
        token_path=str(token_path),
        profile=name,
        code_verifier=verifier,
    )
    return auth_url


@router.post("/{name}/google/auth-url-callback")
async def google_auth_url_callback(
    name: str, req: GoogleCallbackStartRequest
) -> GoogleAuthUrlResponse:
    """Build a consent URL that redirects back to the dashboard to finish on its own.

    The redirect target is the dashboard's own callback (reached through its
    ``/inkwell`` proxy), so the dashboard exchanges the code server-side — no dead
    page, no pasting. A Web client registers that callback for any origin; a Desktop
    client only redirects to loopback, so off-loopback the frontend keeps copy-paste.
    """
    callback_url = validated_callback(req.callback_url)
    auth_url = await start_consent(name, callback_url, use_pkce=True)
    return GoogleAuthUrlResponse(auth_url=auth_url)


@router.post("/{name}/google/auth-url")
async def google_auth_url(
    name: str, req: GoogleCallbackStartRequest
) -> GoogleAuthUrlResponse:
    """Build the copy-paste consent URL, the fallback when the callback can't finish.

    A Web client still points at the dashboard callback (paste-able when the approving
    browser can't reach it back); a Desktop client lands on a dead ``localhost`` page
    the user reads the code off of. PKCE is off — a bare pasted code carries no
    ``state`` to recover a verifier by.
    """
    creds_path, _ = google_paths_for_profile(name)
    if not creds_path.exists():
        raise HTTPException(
            status_code=400, detail="Upload OAuth client credentials JSON first."
        )
    redirect_uri = manual_redirect_for(str(creds_path), req.callback_url)
    auth_url = await start_consent(name, redirect_uri, use_pkce=False)
    return GoogleAuthUrlResponse(auth_url=auth_url)


@router.post("/{name}/google/auth-complete")
async def google_auth_complete(
    name: str, req: GoogleAuthCompleteRequest
) -> GoogleStatusResponse:
    """Exchange the pasted code for a token, against the redirect it was issued for."""
    from oauthlib.oauth2 import OAuth2Error

    from inkwell.agent.google_auth import exchange_code

    creds_path, token_path = google_paths_for_profile(name)
    code = extract_code(req.code)
    if not code:
        raise HTTPException(status_code=400, detail="Paste the authorization code.")
    redirect_uri = manual_redirect_for(str(creds_path), req.callback_url)
    try:
        await asyncio.to_thread(
            exchange_code, str(creds_path), redirect_uri, code, str(token_path)
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OAuth2Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Code rejected: {exc}") from exc
    write_env_local(
        {
            "GOOGLE_CREDENTIALS_PATH": str(creds_path),
            "GOOGLE_TOKEN_PATH": str(token_path),
        },
        profile=name,
    )
    return build_google_status(name)


def callback_page(message: str, *, ok: bool) -> HTMLResponse:
    """A minimal self-contained page shown in the consent tab after the redirect."""
    color = "#1a7f37" if ok else "#cf222e"
    icon = "&#10003;" if ok else "&#10007;"
    html = (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Google authorization</title>"
        "<div style='font:16px system-ui,sans-serif;margin:18vh auto;max-width:30rem;"
        "text-align:center;color:#1f2328;padding:0 1rem'>"
        f"<div style='font-size:2.5rem;color:{color}'>{icon}</div>"
        f"<p>{escape(message)}</p>"
        "<p style='color:#656d76'>Return to the dashboard tab to continue.</p></div>"
    )
    return HTMLResponse(html)


@callback_router.get("/callback")
async def google_callback(
    state: str = "", code: str = "", error: str = ""
) -> HTMLResponse:
    """Catch Google's consent redirect, exchange the code, and confirm in the tab.

    The browser arrives with ``?code=…`` and inkwell finishes authorization
    server-side, so the user never copies anything. The flow is looked up by
    ``state`` (also the CSRF check) and consumed once; the dashboard tab learns it
    succeeded by polling the Google status it just flipped.
    """
    from oauthlib.oauth2 import OAuth2Error

    from inkwell.agent.google_auth import exchange_code

    if error:
        return callback_page(f"Google reported an error: {error}", ok=False)
    pending = PENDING_GOOGLE_AUTH.pop(state, None)
    if pending is None or not code:
        return callback_page(
            "This authorization link has expired — start again from the dashboard.",
            ok=False,
        )
    try:
        await asyncio.to_thread(
            exchange_code,
            pending.credentials_path,
            pending.redirect_uri,
            code,
            pending.token_path,
            code_verifier=pending.code_verifier or None,
        )
    except (FileNotFoundError, OAuth2Error, ValueError) as exc:
        return callback_page(f"Authorization failed: {exc}", ok=False)
    write_env_local(
        {
            "GOOGLE_CREDENTIALS_PATH": pending.credentials_path,
            "GOOGLE_TOKEN_PATH": pending.token_path,
        },
        profile=pending.profile,
    )
    return callback_page("Authorized — you can close this tab.", ok=True)
