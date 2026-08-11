"""Google OAuth and API service creation for Inkwell.

The client builds its resources dynamically and ships no types, so the slice
of the Docs and Drive APIs this project calls is declared here as protocols:
that is what lets a checker resolve `documents().get` as a resource verb
rather than leave every call site reading into an opaque handle.
"""

import logging
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, Unpack, overload

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as discovery_build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lup.types import JsonObject, JsonValue
from lup.web import google_oauth

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


class CommentQuery(TypedDict):
    """The parameters a ``comments().list`` call takes."""

    fileId: str
    fields: str
    pageSize: NotRequired[int]
    pageToken: NotRequired[str]


class CommentsResource(Protocol):
    """The Drive ``comments()`` resource, as this project calls it."""

    def list(self, **query: Unpack[CommentQuery]) -> GoogleRequest: ...

    def create(
        self, *, fileId: str, body: JsonObject, fields: str
    ) -> GoogleRequest: ...


class RepliesResource(Protocol):
    """The Drive ``replies()`` resource, as this project calls it."""

    def create(
        self, *, fileId: str, commentId: str, body: JsonObject, fields: str
    ) -> GoogleRequest: ...


class PermissionsResource(Protocol):
    """The Drive ``permissions()`` resource, as this project calls it."""

    def create(
        self, *, fileId: str, body: JsonObject, sendNotificationEmail: bool = True
    ) -> GoogleRequest: ...


class FilesResource(Protocol):
    """The Drive ``files()`` resource, as this project calls it."""

    def create(
        self, *, body: JsonObject, media_body: MediaFileUpload, fields: str
    ) -> GoogleRequest: ...

    def update(self, *, fileId: str, body: JsonObject) -> GoogleRequest: ...

    def list(self, *, pageSize: int) -> GoogleRequest: ...


class DriveService(Protocol):
    """Google Drive API v3, as this project calls it."""

    def comments(self) -> CommentsResource: ...

    def replies(self) -> RepliesResource: ...

    def permissions(self) -> PermissionsResource: ...

    def files(self) -> FilesResource: ...


@overload
def build(
    serviceName: Literal["docs"], version: Literal["v1"], *, credentials: Credentials
) -> DocsService: ...


@overload
def build(
    serviceName: Literal["drive"], version: Literal["v3"], *, credentials: Credentials
) -> DriveService: ...


def build(
    serviceName: str, version: str, *, credentials: Credentials
) -> DocsService | DriveService:
    """The discovery client, resolved to whichever surface was asked for.

    The client builds its resources at runtime and ships no types, so every call
    site would otherwise read into an opaque handle — and a verb like
    ``documents().get`` would look like a mapping lookup rather than the resource
    method it is. Binding the service name to its protocol here settles that once,
    for every caller, instead of at each one.
    """
    service: DocsService | DriveService = discovery_build(
        serviceName, version, credentials=credentials
    )
    return service


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


class GoogleErrorDetail(BaseModel):
    """One entry in a Google error's ``details`` list."""

    model_config = ConfigDict(extra="ignore")

    reason: str = ""


class GoogleError(BaseModel):
    """The ``error`` object a Google API returns beside a 4xx status."""

    model_config = ConfigDict(extra="ignore")

    details: list[GoogleErrorDetail] = Field(default_factory=list)


class GoogleErrorEnvelope(BaseModel):
    """A Google API error body, which wraps everything under one ``error`` key."""

    model_config = ConfigDict(extra="ignore")

    error: GoogleError = Field(default_factory=GoogleError)


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
        envelope = GoogleErrorEnvelope.model_validate_json(body)
    except (UnicodeDecodeError, ValidationError):
        return False
    return any(d.reason == "SERVICE_DISABLED" for d in envelope.error.details)


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
) -> google_oauth.Consent:
    """Build inkwell's Docs/Drive consent URL for ``redirect_uri``.

    Binds :data:`SCOPES` onto :func:`lup.web.google_oauth.build_consent_url`, which
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

    Binds :data:`SCOPES` onto :func:`lup.web.google_oauth.exchange_code` and persists
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
        cached = self.cached_docs
        if cached is not None:
            return cached
        service: DocsService = build("docs", "v1", credentials=self.credentials())
        self.cached_docs = service
        return service

    def drive_service(self) -> DriveService:
        """Google Drive API v3 service (lazy, cached)."""
        cached = self.cached_drive
        if cached is not None:
            return cached
        service: DriveService = build("drive", "v3", credentials=self.credentials())
        self.cached_drive = service
        return service

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
