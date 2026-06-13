"""build_client must route session-scoped env into the spawned subprocess.

The Agent SDK decides which account pays for inference from the subprocess
environment, so client_env entries (e.g. a per-profile CLAUDE_CONFIG_DIR)
must land in ClaudeAgentOptions.env — otherwise every session bills the
ambient login regardless of the selected profile.
"""

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from lup.client import build_client, client_env

captured: dict[str, ClaudeAgentOptions] = {}


class FakeClient:
    def __init__(self, *, options: ClaudeAgentOptions) -> None:
        captured["options"] = options

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_client_env_merged_into_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr("lup.client.ClaudeSDKClient", FakeClient)
    token = client_env.set({"CLAUDE_CONFIG_DIR": "/tmp/cesia"})
    try:
        async with build_client(model="claude-opus-4-6"):
            pass
    finally:
        client_env.reset(token)
    assert captured["options"].env["CLAUDE_CONFIG_DIR"] == "/tmp/cesia"


async def test_no_client_env_leaves_config_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr("lup.client.ClaudeSDKClient", FakeClient)
    async with build_client(model="claude-opus-4-6"):
        pass
    assert "CLAUDE_CONFIG_DIR" not in captured["options"].env
