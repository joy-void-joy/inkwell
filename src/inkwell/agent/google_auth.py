"""Google OAuth and API service creation for Inkwell."""

# claude: ignore
# googleapiclient.discovery.Resource is untyped — opaque handles throughout.

import json
import logging
from pathlib import Path

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from lup import google_oauth

logger = logging.getLogger(__name__)

type DocsService = object
type DriveService = object

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]


class GoogleAuthError(RuntimeError):
    """Stored Google OAuth credentials are absent or no longer refreshable.

    Raised when the token is missing, carries no refresh token, or the refresh
    is rejected (expired or revoked). A distinct type lets callers surface a
    re-authorize prompt instead of treating it as an opaque runtime failure.
    """


def load_credentials(token_path: str) -> Credentials:
    """Load and refresh stored OAuth credentials.

    Raises GoogleAuthError if no token exists, it has no refresh token, or the
    refresh is rejected — the user must run `inkwell setup` to re-authenticate.
    """
    token = Path(token_path)
    if not token.exists():
        msg = (
            f"No Google token found at {token_path}. "
            "Run `inkwell setup` to authenticate with Google."
        )
        raise GoogleAuthError(msg)

    creds = Credentials.from_authorized_user_file(str(token), SCOPES)

    if creds.valid:
        return creds

    if not creds.refresh_token:
        msg = (
            f"Token at {token_path} has no refresh token. "
            "Run `inkwell setup` to re-authenticate."
        )
        raise GoogleAuthError(msg)

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        msg = (
            f"Failed to refresh Google token: {exc}. "
            "Run `inkwell setup` to re-authenticate."
        )
        raise GoogleAuthError(msg) from exc

    token.write_text(creds.to_json())
    logger.info("Refreshed Google OAuth token at %s", token_path)

    return creds


def is_service_disabled_body(content: bytes | str) -> bool:
    """Whether a Google API 403 body reports SERVICE_DISABLED (API not enabled).

    SERVICE_DISABLED means the API is switched off on the OAuth client's Cloud
    project — distinct from an auth failure and, crucially, not fixable by
    re-authorizing: enabling an API is a project-level toggle, independent of the
    token's scopes. Classifying it lets the status say "enable the API" instead of
    a misleading "authorized".
    """
    try:
        body = content.decode() if isinstance(content, bytes) else content
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return False
    return any(
        isinstance(d, dict) and d.get("reason") == "SERVICE_DISABLED" for d in details
    )


def probe_disabled_apis(token_path: str) -> list[str] | None:
    """Which APIs Inkwell needs are switched off on the OAuth client's project.

    Returns the disabled service hostnames (e.g. ``["docs.googleapis.com"]``), an
    empty list when Docs and Drive both answer, or None when enablement can't be
    determined (no refreshable token, or a transport error). A token existing only
    proves consent was granted; Doc creation still 403s with SERVICE_DISABLED until
    the APIs are enabled on the project, so the status probes them directly rather
    than inferring readiness from the token file.
    """
    try:
        creds = load_credentials(token_path)
        docs_req = (
            build("docs", "v1", credentials=creds).documents().get(documentId="0")
        )
        drive_req = build("drive", "v3", credentials=creds).files().list(pageSize=1)
    except (GoogleAuthError, OSError, RefreshError, httplib2.HttpLib2Error):
        return None

    disabled: list[str] = []
    for service, request in (
        ("docs.googleapis.com", docs_req),
        ("drive.googleapis.com", drive_req),
    ):
        try:
            request.execute()
        except HttpError as exc:
            if is_service_disabled_body(exc.content):
                disabled.append(service)
        except (OSError, RefreshError, httplib2.HttpLib2Error):
            return None
    return disabled


def run_oauth_flow(credentials_path: str, token_path: str) -> Credentials:
    """Run the browser-based OAuth consent flow and persist the token."""
    creds_file = Path(credentials_path)
    if not creds_file.exists():
        msg = (
            f"No credentials file at {credentials_path}. "
            "Download your OAuth client credentials from the Google Cloud Console."
        )
        raise FileNotFoundError(msg)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    result = flow.run_local_server(port=0)
    if not isinstance(result, Credentials):
        msg = "OAuth flow returned unexpected credential type"
        raise RuntimeError(msg)
    creds = result

    token = Path(token_path)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    logger.info("Saved Google OAuth token to %s", token_path)

    return creds


def build_consent_url(
    credentials_path: str,
    redirect_uri: str,
    *,
    state: str | None = None,
    use_pkce: bool = True,
) -> tuple[str, str, str]:
    """Build inkwell's Docs/Drive consent URL for ``redirect_uri``.

    Binds :data:`SCOPES` onto :func:`lup.google_oauth.build_consent_url`, which
    carries the PKCE ``code_verifier`` back for :func:`exchange_code` to present.
    """
    return google_oauth.build_consent_url(
        credentials_path, redirect_uri, SCOPES, state=state, use_pkce=use_pkce
    )


def exchange_code(
    credentials_path: str,
    redirect_uri: str,
    code: str,
    token_path: str,
    *,
    code_verifier: str | None = None,
) -> Credentials:
    """Exchange an authorization code for a saved token, for ``redirect_uri``.

    Binds :data:`SCOPES` onto :func:`lup.google_oauth.exchange_code` and persists
    the returned credentials to ``token_path``. ``code_verifier`` must be the one
    :func:`build_consent_url` returned whenever the consent URL carried a PKCE
    challenge.
    """
    creds = google_oauth.exchange_code(
        credentials_path, redirect_uri, code, SCOPES, code_verifier=code_verifier
    )
    token = Path(token_path)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    logger.info("Saved Google OAuth token to %s", token_path)
    return creds


class ServiceFactory:
    """Lazy-initializing factory for Google API service clients."""

    def __init__(self, token_path: str) -> None:
        self.token_path = token_path
        self.cached_docs: DocsService | None = None
        self.cached_drive: DriveService | None = None

    def credentials(self) -> Credentials:
        """Load credentials, refreshing if expired."""
        return load_credentials(self.token_path)

    def docs_service(self) -> DocsService:
        """Google Docs API v1 service (lazy, cached)."""
        if self.cached_docs is None:
            creds = self.credentials()
            self.cached_docs = build("docs", "v1", credentials=creds)
        return self.cached_docs

    def drive_service(self) -> DriveService:
        """Google Drive API v3 service (lazy, cached)."""
        if self.cached_drive is None:
            creds = self.credentials()
            self.cached_drive = build("drive", "v3", credentials=creds)
        return self.cached_drive

    def invalidate(self) -> None:
        """Clear cached services, forcing re-creation on next access."""
        self.cached_docs = None
        self.cached_drive = None


def get_service_factory() -> ServiceFactory:
    """Create a ServiceFactory from application settings."""
    from inkwell.agent.config import current_settings

    settings = current_settings()

    if not settings.google_credentials_path:
        msg = (
            "GOOGLE_CREDENTIALS_PATH not set. "
            "Run `inkwell setup` to configure Google integration."
        )
        raise RuntimeError(msg)

    if not settings.google_token_path:
        msg = (
            "GOOGLE_TOKEN_PATH not set. "
            "Run `inkwell setup` to configure Google integration."
        )
        raise RuntimeError(msg)

    return ServiceFactory(token_path=settings.google_token_path)
