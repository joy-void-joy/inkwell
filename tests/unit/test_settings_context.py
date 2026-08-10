"""Tests for session-scoped settings — concurrent sessions must not race."""

import asyncio

import inkwell.agent.config as config_mod
from inkwell.agent.config import (
    Settings,
    active_settings,
    current_settings,
    stage_model,
    subprocess_auth_env,
    use_settings,
)
from inkwell.agent.client import client_env


def make_settings(**overrides: str) -> Settings:
    s = Settings.model_validate({})
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


class TestSettingsContext:
    def test_falls_back_to_process_default(self) -> None:
        assert current_settings() is config_mod.settings

    async def test_concurrent_sessions_see_their_own_config(self) -> None:
        observed: dict[str, list[str]] = {"a": [], "b": []}

        async def session(name: str, model: str, writer_mode: str) -> None:
            token = active_settings.set(
                make_settings(model=model, writer_mode=writer_mode)
            )
            try:
                observed[name].append(stage_model("write"))
                await asyncio.sleep(0.01)
                observed[name].append(current_settings().writer_mode)
                await asyncio.sleep(0.01)
                observed[name].append(current_settings().model)
            finally:
                active_settings.reset(token)

        await asyncio.gather(
            asyncio.create_task(session("a", "claude-fable-5", "single")),
            asyncio.create_task(session("b", "claude-opus-4-6", "parallel")),
        )

        assert observed["a"] == ["claude-fable-5", "single", "claude-fable-5"]
        assert observed["b"] == ["claude-opus-4-6", "parallel", "claude-opus-4-6"]

    async def test_child_tasks_inherit_session_settings(self) -> None:
        token = active_settings.set(make_settings(model="claude-fable-5"))
        try:

            async def child() -> str:
                return stage_model("rewrite")

            result = await asyncio.create_task(child())
        finally:
            active_settings.reset(token)

        assert result == "claude-fable-5"
        assert current_settings() is config_mod.settings

    async def test_stage_override_wins_inside_context(self) -> None:
        s = make_settings(model="claude-opus-4-6")
        s.stage_models = {"reader": "claude-haiku-4-5-20251001"}
        token = active_settings.set(s)
        try:
            assert stage_model("reader") == "claude-haiku-4-5-20251001"
            assert stage_model("plan") == "claude-opus-4-6"
        finally:
            active_settings.reset(token)


class TestSubprocessAuthEnv:
    def test_claude_login_is_routed(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        s.openrouter_api_key = None
        assert subprocess_auth_env(s) == {"CLAUDE_CONFIG_DIR": "/tmp/cesia"}

    def test_openrouter_is_routed(self) -> None:
        s = make_settings(openrouter_api_key="sk-or-xyz")
        s.claude_config_dir = None
        env = subprocess_auth_env(s)
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-xyz"
        assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert env["ANTHROPIC_API_KEY"] == ""

    def test_empty_without_credentials(self) -> None:
        s = make_settings()
        s.claude_config_dir = None
        s.openrouter_api_key = None
        assert subprocess_auth_env(s) == {}


class TestUseSettings:
    def test_scopes_settings_and_subprocess_env(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        s.openrouter_api_key = None
        with use_settings(s):
            assert current_settings() is s
            assert client_env.get() == {"CLAUDE_CONFIG_DIR": "/tmp/cesia"}
        assert current_settings() is config_mod.settings
        assert client_env.get() is None

    def test_resets_both_on_exception(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        try:
            with use_settings(s):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert client_env.get() is None
        assert current_settings() is config_mod.settings
