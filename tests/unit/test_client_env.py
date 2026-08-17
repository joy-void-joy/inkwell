"""Session-scoped env must reach the session the runtime opens.

The runtime runs its CLI as a subprocess that decides which account pays for
inference from its own environment, so a per-profile config home set on
``client_env`` has to land in the request — otherwise every session bills the
ambient login regardless of the profile selected for it.

The recording runtime stands in for whichever one inkwell selects, so this
pins inkwell's own wiring rather than one provider's config shape.
"""

import pytest

from lup.runtime.factory import SessionFactory
from lup.runtime.login import ProviderLogin
from lup.runtime.selection import Runtime, SessionRequest

import inkwell.agent.client as client

captured: dict[str, SessionRequest] = {}

CONFIG_HOME = client.PROVIDER_LOGIN.config_home_env


def capture_request(request: SessionRequest) -> SessionFactory:
    """Stand in for a runtime, keeping the request it was handed."""
    captured["request"] = request
    return SessionFactory(lambda resume=None: None)  # pyright: ignore[reportArgumentType]


RECORDING_RUNTIME = Runtime(
    name="recording",
    login=ProviderLogin(
        config_home_env=CONFIG_HOME,
        credentials_file="creds.json",
        home_subdir="recording-home",
    ),
    open=capture_request,
)


def test_client_env_reaches_the_session_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr(client, "RUNTIME", RECORDING_RUNTIME)

    token = client.client_env.set({CONFIG_HOME: "/tmp/cesia"})
    try:
        client.provider_factory(model="claude-opus-5")
    finally:
        client.client_env.reset(token)

    assert captured["request"].environment[CONFIG_HOME] == "/tmp/cesia"


def test_no_client_env_leaves_the_config_home_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr(client, "RUNTIME", RECORDING_RUNTIME)

    client.provider_factory(model="claude-opus-5")

    assert CONFIG_HOME not in captured["request"].environment
