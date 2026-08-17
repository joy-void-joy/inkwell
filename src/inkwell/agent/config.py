"""Configuration for the inkwell writing agent."""

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self, get_args

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from lup.types import EnvVars

from inkwell.agent.book import BookStore
from inkwell.agent.client import PROVIDER_LOGIN
from inkwell.corpus.semantics import DEFAULT_LOCAL_MODEL, SemanticLayer
from inkwell.corpus.storage import CorpusStore

logger = logging.getLogger(__name__)


class ProfileSelection(BaseSettings):
    """Which profile the environment asks for.

    Declares no `env_file`, so it reads the process environment and nothing
    else — which is what keeps it clear of the circularity its own result
    would otherwise create, since the profile chooses the env file chain
    `Settings` reads and a profile read from one of those files could not.
    """

    model_config = SettingsConfigDict(extra="ignore")

    profile: str | None = Field(default=None, validation_alias="INKWELL_PROFILE")


class ProfileOverride(BaseModel):
    """A profile chosen in-process, overriding what the environment asked for."""

    name: str | None = None


override = ProfileOverride()
"""Set by an entry point that took a profile as an argument, so that the loads
which follow resolve against it. A holder rather than a rebound module-level
name, so `select_profile` needs no `global`."""


def profile_store_root() -> Path:
    """The checkout holding ``profiles/``, shared by every worktree of this repo.

    A profile is an account store rather than a per-branch artifact, and
    ``profiles/`` is gitignored, so it exists only in the main working tree.
    Resolving it against the running checkout instead would find nothing from a
    worktree and fall back to the unprofiled defaults — billing whichever login
    the process inherited and dropping every key the profile holds, silently.

    A worktree's ``.git`` is the gitfile naming the shared directory, so the
    main tree is two levels above what it points at.
    """
    checkout = Path(__file__).resolve().parents[3]
    gitfile = checkout / ".git"
    if not gitfile.is_file():
        return checkout
    gitdir = gitfile.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
    return Path(gitdir).resolve().parents[2]


PROFILES_DIR = profile_store_root() / "profiles"
"""Where every profile directory lives, for whichever checkout is running."""

CORPUS_DIR = profile_store_root() / "corpus"
"""Where the research corpus lives unless configured otherwise.

Beside ``profiles/`` rather than inside a session, because the corpus is the one
artifact here that is expensive to build and worth nothing if it is rebuilt per
run: a session reads what earlier runs already enumerated. Resolving it against
the shared checkout also means every worktree reads one corpus instead of each
re-scraping the same sources.
"""

BOOKS_DIR = profile_store_root() / "books"
"""Where a book's cross-chapter record lives unless configured otherwise.

Beside ``corpus/`` and for the same reason. A book is written one chapter per
run, so a record kept in the session notes tree would die with the run that
made it and chapter nine would have nothing of chapter one left to read.
Resolving it against the shared checkout also means every worktree reads one
book, rather than each starting a fresh one under whichever branch it is on.
"""

ACTIVE_PROFILE_FILE = PROFILES_DIR / ".active"
"""Where selecting a profile records it, for the runs that name none.

Consulted last, so an explicit argument and ``INKWELL_PROFILE`` both still
win over it. It sits beside the profiles themselves rather than in an env
file, because the selection is what decides which env files are read.
"""


def recorded_profile() -> str | None:
    """The profile a previous selection recorded, where one did."""
    if not ACTIVE_PROFILE_FILE.exists():
        return None
    return ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip() or None


def active_profile() -> str | None:
    """Return the active profile name, or None for the default config."""
    return override.name or ProfileSelection().profile or recorded_profile()


def select_profile(profile: str | None) -> None:
    """Make `profile` the active one for the loads that follow."""
    override.name = profile


def env_files_for(profile: str | None) -> tuple[str, ...]:
    """The env file chain a profile reads, most general first."""
    if profile:
        return (".env", str(PROFILES_DIR / profile / "env"))
    return (".env", ".env.local")


def build_env_files() -> tuple[str, ...]:
    """Build the env file chain based on the active profile."""
    return env_files_for(active_profile())


type PipelineStage = Literal[
    "preprocess",
    "extract",
    "voice",
    "book",
    "plan",
    "assumptions",
    "research",
    "refine",
    "write",
    "merge",
    "review",
    "resolve",
    "rewrite",
    "classify",
    "orchestrate",
    "reader",
    "format",
]
"""One stage of the writing pipeline, as a per-stage model override names it."""

PIPELINE_STAGES: tuple[PipelineStage, ...] = get_args(PipelineStage.__value__)
"""Stage names accepted by per-stage model overrides, in pipeline order."""

SUGGESTED_MODELS: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)
"""Model ids offered in pickers; any model id string is accepted."""

WRITER_MODES: tuple[str, ...] = ("auto", "parallel", "single")
"""Draft production modes for the write stage."""


class Settings(BaseSettings):
    """Inkwell settings loaded from environment variables."""

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Resolve the env file chain when an instance is built rather than when
        the class is defined, so a profile selected at runtime is what it reads."""
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=build_env_files()),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def warn_missing_optional_keys(self) -> Self:
        missing = []
        if not self.exa_api_key:
            missing.append("EXA_API_KEY (Exa search disabled)")
        if not self.claude_cookie:
            missing.append("CLAUDE_COOKIE (Claude conversation extraction disabled)")
        if missing:
            logger.warning("Missing optional keys: %s", ", ".join(missing))
        return self

    # ==========================================================================
    # GOOGLE (OAuth — configured via `inkwell setup`)
    # ==========================================================================

    google_credentials_path: str | None = Field(
        default=None,
        validation_alias="GOOGLE_CREDENTIALS_PATH",
        description="Path to Google OAuth credentials JSON",
    )

    google_token_path: str | None = Field(
        default=None,
        validation_alias="GOOGLE_TOKEN_PATH",
        description="Path to stored Google OAuth token",
    )

    author_email: str | None = Field(
        default=None,
        validation_alias="INKWELL_AUTHOR_EMAIL",
        description="Author's email for Google Doc sharing (editor access)",
    )

    base_path: str = Field(
        default="/",
        validation_alias="INKWELL_BASE_PATH",
        description="Sub-path the dashboard is served under behind a reverse proxy",
    )

    google_workspace_domain: str | None = Field(
        default=None,
        validation_alias="INKWELL_GOOGLE_WORKSPACE_DOMAIN",
        description="Google Workspace domain for org-wide edit sharing (e.g. 'example.com')",
    )

    # ==========================================================================
    # RESEARCH API KEYS
    # ==========================================================================

    exa_api_key: str | None = Field(
        default=None,
        validation_alias="EXA_API_KEY",
        description="Exa AI search API key",
    )

    fred_api_key: str | None = Field(
        default=None,
        validation_alias="FRED_API_KEY",
        description="FRED economic data API key",
    )

    # ==========================================================================
    # EXTRACTION
    # ==========================================================================

    claude_cookie: str | None = Field(
        default=None,
        validation_alias="CLAUDE_COOKIE",
        description="Claude.ai session cookie for conversation extraction",
    )

    claude_org_uuid: str | None = Field(
        default=None,
        validation_alias="CLAUDE_ORG_UUID",
        description="Claude.ai organization UUID (for org-restricted shares)",
    )

    # ==========================================================================
    # LLM ROUTING
    # ==========================================================================

    claude_config_dir: str | None = Field(
        default=None,
        validation_alias=PROVIDER_LOGIN.config_home_env,
        description="Separate runtime config directory for agent login",
    )

    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias="OPENROUTER_API_KEY",
        description="OpenRouter API key",
    )

    # ==========================================================================
    # MODEL SETTINGS
    # ==========================================================================

    model: str = Field(
        default="claude-opus-5",
        validation_alias="AGENT_MODEL",
        description="Default Claude model for all pipeline stages",
    )

    stage_models: dict[PipelineStage, str] = Field(
        default_factory=dict,
        validation_alias="AGENT_STAGE_MODELS",
        description=(
            "Per-stage model overrides as JSON, e.g. "
            '{"write": "claude-fable-5", "reader": "claude-haiku-4-5"}. '
            f"Stages: {', '.join(PIPELINE_STAGES)}. "
            "Unlisted stages use AGENT_MODEL."
        ),
    )

    writer_mode: str = Field(
        default="auto",
        validation_alias="AGENT_WRITER_MODE",
        description=(
            "Draft production mode: 'parallel' = one writer per section "
            "sharing a glossary, assembled by a voice-safe reconcile pass; "
            "'single' = one writer drafts the whole piece in order (no "
            "reconcile pass); 'auto' = parallel for every format (the "
            "reconcile pass keeps the author's voice intact)"
        ),
    )

    def model_for(self, stage: PipelineStage) -> str:
        """Model for a pipeline stage: stage override, else the default model."""
        return self.stage_models[stage] if stage in self.stage_models else self.model

    max_thinking_tokens: int | None = Field(
        default=128_000 - 1,
        validation_alias="AGENT_MAX_THINKING_TOKENS",
        description="Max thinking tokens",
    )

    # ==========================================================================
    # PATHS
    # ==========================================================================

    notes_path: str = Field(
        default="./notes",
        validation_alias="AGENT_NOTES_PATH",
        description="Base path for notes",
    )

    logs_path: str = Field(
        default="./logs",
        validation_alias="AGENT_LOGS_PATH",
        description="Base path for logs",
    )

    style_corpus_path: str = Field(
        default="./config/style",
        validation_alias="INKWELL_STYLE_CORPUS_PATH",
        description="Path to style reference corpus",
    )

    reader_feedback_path: str | None = Field(
        default=None,
        validation_alias="INKWELL_READER_FEEDBACK_PATH",
        description=(
            "Reader-feedback export to ingest at session start — one JSON file, "
            "or a directory of them. Feedback from readers of already-published "
            "text, filed per section for the stages that revise it."
        ),
    )

    corpus_path: str = Field(
        default=str(CORPUS_DIR),
        validation_alias="INKWELL_CORPUS_PATH",
        description=(
            "Where the research corpus is stored — enumerated documents and "
            "one index per source. Shared across sessions and worktrees, so a "
            "run reads what earlier ingestion already gathered."
        ),
    )

    books_path: str = Field(
        default=str(BOOKS_DIR),
        validation_alias="INKWELL_BOOKS_PATH",
        description=(
            "Where cross-chapter book records are stored — one directory per "
            "book, one file per chapter. Shared across sessions and worktrees, "
            "so a chapter run reads what earlier chapter runs wrote."
        ),
    )

    corpus_embeddings: bool = Field(
        default=False,
        validation_alias="INKWELL_CORPUS_EMBEDDINGS",
        description=(
            "Whether the corpus's optional semantic layer answers nearest-"
            "neighbour queries. Off by default: browsing, filtering, and "
            "ordering are structural and need no vectors, so the layer is worth "
            "switching on for the question that shares no vocabulary with the "
            "corpus and costs nothing to leave off"
        ),
    )

    corpus_embedding_model: str = Field(
        default=DEFAULT_LOCAL_MODEL,
        validation_alias="INKWELL_CORPUS_EMBEDDING_MODEL",
        description=(
            "Which model the semantic layer embeds with when it is switched on. "
            "A local static-embedding model by default, so enabling the layer "
            "needs no API key and no per-query network call"
        ),
    )

    # ==========================================================================
    # LIMITS
    # ==========================================================================

    max_budget_usd: float | None = Field(
        default=None,
        validation_alias="AGENT_MAX_BUDGET_USD",
        description="Maximum budget per session",
    )

    max_turns: int | None = Field(
        default=None,
        validation_alias="AGENT_MAX_TURNS",
        description="Maximum agent turns per session",
    )

    http_timeout_seconds: int = Field(
        default=30,
        validation_alias="AGENT_HTTP_TIMEOUT_SECONDS",
        description="HTTP request timeout",
    )

    sandbox_timeout_seconds: int = Field(
        default=30,
        validation_alias="AGENT_SANDBOX_TIMEOUT_SECONDS",
        description="Sandbox code execution timeout",
    )

    max_concurrent_requests: int = Field(
        default=5,
        validation_alias="AGENT_MAX_CONCURRENT_REQUESTS",
        description="Max concurrent external API requests",
    )

    # ==========================================================================
    # PROFILE
    # ==========================================================================

    profile: str | None = Field(
        default=None,
        validation_alias="INKWELL_PROFILE",
        description="Active configuration profile name",
    )


@contextmanager
def profile_selected(profile: str | None) -> Iterator[None]:
    """Run a block with `profile` active, restoring whatever was active before."""
    previous = active_profile()
    select_profile(profile)
    try:
        yield
    finally:
        select_profile(previous)


def load_settings(profile: str | None = None) -> Settings:
    """Create a Settings instance for the given profile.

    The profile has to be active while the instance is built, because it is
    what `build_env_files` resolves the env file chain from.
    """
    with profile_selected(profile):
        return Settings()


settings = Settings.model_validate({})

active_settings: contextvars.ContextVar[Settings | None] = contextvars.ContextVar(
    "active_settings", default=None
)
"""Session-scoped settings override.

Each asyncio task tree gets its own contextvar copy, so concurrent
sessions launched with different profiles or model configurations read
their own Settings instead of racing on the module global. Set it at
the root of a session task (see the web SessionManager); everything the
session spawns inherits it.
"""


def current_settings() -> Settings:
    """Settings for the current execution context.

    Session-scoped code must read configuration through this — the
    module-level ``settings`` is only the process default and is wrong
    whenever sessions run concurrently with different configurations.
    """
    override = active_settings.get()
    return override if override is not None else settings


def stage_model(stage: PipelineStage) -> str:
    """Model for a pipeline stage from the active settings."""
    return current_settings().model_for(stage)


def corpus_root() -> Path:
    """Where the research corpus lives for the current execution context."""
    return Path(current_settings().corpus_path).expanduser()


def book_store() -> BookStore:
    """The cross-chapter book records for the current execution context.

    Resolved here rather than held by a run, because the store outlives every
    run that writes to it: a chapter reaches it the same way whether it is the
    first of its book or the ninth.
    """
    return BookStore(root=Path(current_settings().books_path).expanduser())


def corpus_semantics(store: CorpusStore) -> SemanticLayer:
    """The corpus's semantic layer for the current context.

    Switched on by configuration rather than by any code path deciding it is
    time: retrieval holds this whether or not it can answer, and asks it, and
    carries on with the structural result when it says it cannot.
    """
    return SemanticLayer(store=store, enabled=current_settings().corpus_embeddings)


def subprocess_auth_env(session_settings: Settings) -> EnvVars:
    """Auth env routing a session's spawned ``claude`` CLI to its profile.

    The CLI decides which account pays for inference from its own
    environment, so a profile only affects billing if this reaches the
    subprocess. Without it, every session inherits the server's ambient
    login and bills that account regardless of the selected profile.

    Routing through a compatible endpoint is not here: that is a transform
    over the runtime's own configuration, applied in ``agent.client``.
    """
    if not session_settings.claude_config_dir:
        return {}
    return {PROVIDER_LOGIN.config_home_env: session_settings.claude_config_dir}


@contextmanager
def use_settings(session_settings: Settings) -> Iterator[None]:
    """Scope a session's settings and subprocess auth env to the task.

    Sets ``active_settings`` so ``current_settings()`` reflects the profile,
    and lup's ``client_env`` so the spawned ``claude`` CLI bills the
    profile's account. Both the web and CLI entry points wrap session
    execution in this, so profile selection is honored end to end.
    """
    from inkwell.agent.client import client_env

    settings_token = active_settings.set(session_settings)
    env_token = client_env.set(subprocess_auth_env(session_settings))
    try:
        yield
    finally:
        client_env.reset(env_token)
        active_settings.reset(settings_token)
