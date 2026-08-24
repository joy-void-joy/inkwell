"""Session-scoped env must reach the session the runtime opens.

The runtime runs its CLI as a subprocess that decides which account pays for
inference from its own environment, so the config home a run resolves has to
land in the request — otherwise every session bills the ambient login
regardless of the profile selected for it. Two ways in, and both are here: an
environment a caller set outright, and the one the settings in scope imply.

The recording runtime stands in for whichever one inkwell selects, so this
pins inkwell's own wiring rather than one provider's config shape.
"""

import pytest

from lup.runtime.factory import SessionFactory
from lup.runtime.login import ProviderLogin
from lup.runtime.selection import Runtime, SessionRequest

import inkwell.agent.client as client
from inkwell.agent.config import Settings, use_settings

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
    # Contains nothing, so what the request carries is what inkwell put there.
    # A stand-in that routed the config home at a workspace of its own would
    # make the unset case assert against its own injection instead.
    workspace_home=lambda environment, _workspace: environment,
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


def test_the_active_settings_route_a_session_nobody_set_an_env_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that scoped its profile and nothing else still bills that profile.

    The entry points that open sessions do not all set an environment, and the
    one that scoped its settings alone used to authenticate as whatever login
    its launching shell exported — so what the settings name has to be enough.
    """
    captured.clear()
    monkeypatch.setattr(client, "RUNTIME", RECORDING_RUNTIME)

    scoped = Settings.model_validate({})
    scoped.claude_config_dir = "/tmp/cesia"
    with use_settings(scoped):
        client.provider_factory(model="claude-opus-5")

    assert captured["request"].environment[CONFIG_HOME] == "/tmp/cesia"


def test_settings_naming_no_config_home_leave_it_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr(client, "RUNTIME", RECORDING_RUNTIME)

    unprofiled = Settings.model_validate({})
    unprofiled.claude_config_dir = None
    with use_settings(unprofiled):
        client.provider_factory(model="claude-opus-5")

    assert CONFIG_HOME not in captured["request"].environment
