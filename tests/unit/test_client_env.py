"""Session-scoped env must reach the session the provider opens.

The provider runs its CLI as a subprocess that decides which account pays for
inference from its own environment, so a per-profile ``CLAUDE_CONFIG_DIR`` set
on ``client_env`` has to land in the session config — otherwise every session
bills the ambient login regardless of the profile selected for it.
"""

import pytest

from lup.adapters.claude.runtime import ClaudeSessionConfig
from lup.runtime.factory import SessionFactory

import inkwell.agent.client as client

captured: dict[str, ClaudeSessionConfig] = {}


def capture_config(config: ClaudeSessionConfig) -> SessionFactory:
    """Stand in for the adapter, keeping the config it was handed."""
    captured["config"] = config
    return SessionFactory(lambda resume=None: None)  # pyright: ignore[reportArgumentType]


def test_client_env_reaches_the_session_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr(client, "create_claude_session_factory", capture_config)

    token = client.client_env.set({"CLAUDE_CONFIG_DIR": "/tmp/cesia"})
    try:
        client.provider_factory(model="claude-opus-4-6")
    finally:
        client.client_env.reset(token)

    assert captured["config"].environment["CLAUDE_CONFIG_DIR"] == "/tmp/cesia"


def test_no_client_env_leaves_the_config_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr(client, "create_claude_session_factory", capture_config)

    client.provider_factory(model="claude-opus-4-6")

    assert "CLAUDE_CONFIG_DIR" not in captured["config"].environment
