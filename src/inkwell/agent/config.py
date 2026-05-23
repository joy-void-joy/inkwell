"""Configuration for the inkwell writing agent."""

import logging
import os
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Inkwell settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
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
        description="Claude model for main agent",
    )

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


settings = Settings.model_validate({})

if settings.openrouter_api_key:
    os.environ.setdefault("ANTHROPIC_BASE_URL", "https://openrouter.ai/api")
    os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", settings.openrouter_api_key)
    os.environ.setdefault("ANTHROPIC_API_KEY", "")
    logger.info("OpenRouter enabled")
