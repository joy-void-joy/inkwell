"""Google OAuth and API service creation for Inkwell."""

# claude: ignore
# googleapiclient.discovery.Resource is untyped — opaque handles throughout.

import logging
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, Unpack

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from lup.types import JsonValue

from inkwell.agent.markdown_to_docs import BatchUpdateBody, NewDocumentBody

logger = logging.getLogger(__name__)


class GoogleRequest(Protocol):
    """The one thing this project does with a googleapiclient request.

    The client builds its resources dynamically and ships no types, so the
    shape it hands back is named here rather than inferred: every caller
    only ever executes it and reads JSON out.
    """

    def execute(self) -> JsonValue: ...


class DocumentQuery(TypedDict):
    """The parameters a ``documents().get`` call takes."""

    documentId: str
    includeTabsContent: NotRequired[bool]
    suggestionsViewMode: NotRequired[str]


class DocumentsResource(Protocol):
    """The Docs API's ``documents()`` resource, as this project calls it.

    Naming the three verbs used keeps ``get`` legible as this resource's own
    method: a checker resolves it here rather than mistaking it for a lookup
    into a mapping, which is all an untyped handle leaves it looking like.
    """

    def get(self, **query: Unpack[DocumentQuery]) -> GoogleRequest: ...

    def batchUpdate(
        self, *, documentId: str, body: BatchUpdateBody
    ) -> GoogleRequest: ...

    def create(self, *, body: NewDocumentBody) -> GoogleRequest: ...


class DocsService(Protocol):
    """Google Docs API v1, as this project calls it."""

    def documents(self) -> DocumentsResource: ...


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
        cached = self.cached_docs
        if cached is not None:
            return cached
        service: DocsService = build("docs", "v1", credentials=self.credentials())
        self.cached_docs = service
        return service

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
