"""REST endpoints for profile management."""

import asyncio
import json
import shutil

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from inkwell.agent.browser_auth import context_has_cookies, login_interactive
from inkwell.agent.client import PROVIDER_LOGIN, RUNTIME
from inkwell.devtools.setup import (
    PROFILES_DIR,
    claude_config_dir_for_profile,
    credentials_dir_for_profile,
    env_file_for_profile,
    env_value,
    google_paths_for_profile,
    list_profiles,
    mask,
    read_env_local,
    write_env_local,
)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ServerCapabilities(BaseModel):
    has_display: bool = Field(description="Whether server has a graphical display")
    is_local: bool = Field(description="Whether client appears to be on localhost")


class IntegrationStatus(BaseModel):
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
        PROVIDER_LOGIN.config_home_env,
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
    google = google_paths_for_profile(name)

    integrations: list[IntegrationStatus] = []

    config_dir = env_value(env, PROVIDER_LOGIN.config_home_env)
    login_ok = bool(config_dir) and PROVIDER_LOGIN.logged_in(Path(config_dir))
    integrations.append(
        IntegrationStatus(
            name=f"{RUNTIME.name} login",
            configured=login_ok,
            detail=config_dir if login_ok else "not configured",
        )
    )

    google_ok = google.token.exists()
    if google_ok:
        google_detail = "authorized"
    elif google.credentials.exists():
        google_detail = "credentials uploaded, not yet authorized"
    else:
        google_detail = "not configured"
    integrations.append(
        IntegrationStatus(
            name="Google",
            configured=google_ok,
            detail=google_detail,
        )
    )

    exa_key = env.get("EXA_API_KEY", "")
    integrations.append(
        IntegrationStatus(
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
            name="Claude.ai session",
            configured=claude_ok,
            detail=claude_detail,
        )
    )

    fred_key = env.get("FRED_API_KEY", "")
    integrations.append(
        IntegrationStatus(
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
        if d.is_dir() and PROVIDER_LOGIN.logged_in(d)
    ]

    return DetectResult(found=found)


def build_google_status(profile: str) -> GoogleStatusResponse:
    """Check Google credentials and token file status for a profile."""
    google = google_paths_for_profile(profile)
    creds_path, token_path = google.credentials, google.token
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
    )


@router.get("/{name}/google/status")
async def google_status(name: str) -> GoogleStatusResponse:
    """Check Google OAuth status for a profile."""
    return build_google_status(name)


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

    creds_path = google_paths_for_profile(name).credentials
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_bytes(content)
    return build_google_status(name)


@router.post("/{name}/google/authorize")
async def authorize_google(name: str) -> GoogleStatusResponse:
    """Run the Google OAuth browser flow and save the token."""
    from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

    from inkwell.agent.google_auth import run_oauth_flow

    google = google_paths_for_profile(name)
    creds_path, token_path = google.credentials, google.token

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
