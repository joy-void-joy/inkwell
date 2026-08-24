"""Tests for session-scoped settings — concurrent sessions must not race."""

import asyncio

from lup.adapters.claude.config import ClaudeCompatibilityTransform
from lup.adapters.claude.selection import claude_config
from lup.runtime.selection import SessionRequest

import inkwell.agent.config as config_mod
from inkwell.agent.config import (
    Settings,
    active_settings,
    current_settings,
    stage_model,
    subprocess_auth_env,
    use_settings,
)
from inkwell.agent.client import (
    client_env,
    compatible_endpoint,
    session_environment,
)


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
            asyncio.create_task(session("b", "claude-opus-5", "parallel")),
        )

        assert observed["a"] == ["claude-fable-5", "single", "claude-fable-5"]
        assert observed["b"] == ["claude-opus-5", "parallel", "claude-opus-5"]

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
        s = make_settings(model="claude-opus-5")
        s.stage_models = {"reader": "claude-haiku-4-5"}
        token = active_settings.set(s)
        try:
            assert stage_model("reader") == "claude-haiku-4-5"
            assert stage_model("plan") == "claude-opus-5"
        finally:
            active_settings.reset(token)


class TestSubprocessAuthEnv:
    def test_claude_login_is_routed(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        s.openrouter_api_key = None
        assert subprocess_auth_env(s) == {"CLAUDE_CONFIG_DIR": "/tmp/cesia"}

    def test_empty_without_credentials(self) -> None:
        s = make_settings()
        s.claude_config_dir = None
        s.openrouter_api_key = None
        assert subprocess_auth_env(s) == {}

    def test_openrouter_is_not_routed_through_the_environment(self) -> None:
        """Routing is a transform over the session config, not an env var."""
        s = make_settings(openrouter_api_key="sk-or-xyz")
        s.claude_config_dir = None
        assert subprocess_auth_env(s) == {}


class TestCompatibleEndpoint:
    def test_openrouter_key_routes_the_session(self) -> None:
        s = make_settings(openrouter_api_key="sk-or-xyz")
        s.claude_config_dir = None
        with use_settings(s):
            endpoint = compatible_endpoint()
            assert endpoint is not None
            routed = ClaudeCompatibilityTransform(endpoint).apply(
                claude_config(SessionRequest())
            )
        assert routed.environment["ANTHROPIC_AUTH_TOKEN"] == "sk-or-xyz"
        assert routed.environment["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert routed.environment["ANTHROPIC_API_KEY"] == ""

    def test_no_key_leaves_the_vendor_endpoint(self) -> None:
        s = make_settings()
        s.openrouter_api_key = None
        with use_settings(s):
            assert compatible_endpoint() is None

    def test_routing_keeps_the_profile_login(self) -> None:
        """The endpoint transform adds to the session env, it does not replace it."""
        s = make_settings(claude_config_dir="/tmp/cesia", openrouter_api_key="sk-or")
        with use_settings(s):
            endpoint = compatible_endpoint()
            assert endpoint is not None
            routed = ClaudeCompatibilityTransform(endpoint).apply(
                claude_config(SessionRequest(environment=session_environment()))
            )
        assert routed.environment["CLAUDE_CONFIG_DIR"] == "/tmp/cesia"
        assert routed.environment["ANTHROPIC_AUTH_TOKEN"] == "sk-or"


class TestUseSettings:
    def test_scopes_settings_and_the_env_they_imply(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        s.openrouter_api_key = None
        with use_settings(s):
            assert current_settings() is s
            assert session_environment() == {"CLAUDE_CONFIG_DIR": "/tmp/cesia"}
        assert current_settings() is config_mod.settings
        assert session_environment() == subprocess_auth_env(config_mod.settings)

    def test_resets_both_on_exception(self) -> None:
        s = make_settings(claude_config_dir="/tmp/cesia")
        try:
            with use_settings(s):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert session_environment() == subprocess_auth_env(config_mod.settings)
        assert current_settings() is config_mod.settings

    def test_an_env_a_caller_set_wins_over_the_settings(self) -> None:
        """Because the settings imply an account, they do not dictate one."""
        s = make_settings(claude_config_dir="/tmp/cesia")
        token = client_env.set({"CLAUDE_CONFIG_DIR": "/tmp/perso"})
        try:
            with use_settings(s):
                assert session_environment() == {"CLAUDE_CONFIG_DIR": "/tmp/perso"}
        finally:
            client_env.reset(token)
