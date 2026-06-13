"""Configuration for the inkwell writing agent."""

import logging
import os
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def active_profile() -> str | None:
    """Return the active profile name, or None for the default config."""
    return os.environ.get("INKWELL_PROFILE") or None


def build_env_files() -> tuple[str, ...]:
    """Build the env file chain based on the active profile."""
    profile = active_profile()
    if profile:
        return (".env", f"profiles/{profile}/env")
    return (".env", ".env.local")


PIPELINE_STAGES: tuple[str, ...] = (
    "preprocess",
    "extract",
    "voice",
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
)
"""Stage names accepted by per-stage model overrides."""

SUGGESTED_MODELS: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-fable-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)
"""Model ids offered in pickers; any model id string is accepted."""

WRITER_MODES: tuple[str, ...] = ("parallel", "single")
"""Draft production modes for the write stage."""


class Settings(BaseSettings):
    """Inkwell settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=build_env_files(),
        extra="ignore",
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
        validation_alias="CLAUDE_CONFIG_DIR",
        description="Separate Claude config directory for agent login",
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
        default="claude-opus-4-6",
        validation_alias="AGENT_MODEL",
        description="Default Claude model for all pipeline stages",
    )

    stage_models: dict[str, str] = Field(
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
        default="parallel",
        validation_alias="AGENT_WRITER_MODE",
        description=(
            "Draft production mode: 'parallel' = one writer per section "
            "plus a merge stage; 'single' = one writer drafts the whole "
            "piece in order (no merge stage, no cross-section drift)"
        ),
    )

    def model_for(self, stage: str) -> str:
        """Model for a pipeline stage: stage override, else the default model."""
        return self.stage_models.get(stage, self.model)

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


def load_settings(profile: str | None = None) -> Settings:
    """Create a Settings instance for the given profile.

    Temporarily sets INKWELL_PROFILE so build_env_files() resolves
    the correct env file chain, then constructs Settings with that chain.
    """
    import os as _os

    prev = _os.environ.get("INKWELL_PROFILE")
    if profile:
        _os.environ["INKWELL_PROFILE"] = profile
    else:
        _os.environ.pop("INKWELL_PROFILE", None)
    try:
        return Settings(_env_file=build_env_files())  # pyright: ignore[reportCallIssue]
    finally:
        if prev is not None:
            _os.environ["INKWELL_PROFILE"] = prev
        else:
            _os.environ.pop("INKWELL_PROFILE", None)


settings = Settings.model_validate({})


def stage_model(stage: str) -> str:
    """Model for a pipeline stage from the active settings."""
    return settings.model_for(stage)


if settings.openrouter_api_key:
    os.environ.setdefault("ANTHROPIC_BASE_URL", "https://openrouter.ai/api")
    os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", settings.openrouter_api_key)
    os.environ.setdefault("ANTHROPIC_API_KEY", "")
    logger.info("OpenRouter enabled")
