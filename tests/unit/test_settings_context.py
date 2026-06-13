"""Tests for session-scoped settings — concurrent sessions must not race."""

import asyncio

import inkwell.agent.config as config_mod
from inkwell.agent.config import (
    Settings,
    active_settings,
    current_settings,
    stage_model,
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
