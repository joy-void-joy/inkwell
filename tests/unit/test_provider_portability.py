"""What it would take to run a stage on a provider other than the current one.

The argument for a second provider is independence, not price: every reviewer
shares the writer's model family, so a blind spot they hold in common is
invisible from inside the run. Acting on that means knowing what actually
stops a stage from opening on another runtime — and the answer is not the
model name.

Every stage that has tools declares them as an **in-process** server: a live
``Server`` instance the adapter registers inside the running process. Codex
launches each tool group as a **subprocess** and refuses anything else. So a
per-stage runtime switch would open cleanly for a stage with no tools and
raise for every stage that has any, which is nearly all of them.

This pins that, so the port is a thing somebody does rather than a thing they
discover halfway through. What the port needs is a stdio entry point for the
tool servers — the handlers are already pure functions of typed input.
"""

from pathlib import Path

import pytest

from lup.adapters.codex.selection import codex_mcp_server
from lup.mcp import LupMcpServerConfig

from inkwell.agent.glossary import RunGlossary
from inkwell.agent.pipeline import (
    build_glossary_server,
    build_note_server,
    build_output_server,
)
from inkwell.agent.models import AuthorNote
from inkwell.agent.tools.stage_outputs import make_glossary_tools, make_note_tool


def stage_servers() -> list[tuple[str, LupMcpServerConfig]]:
    """One of each shape of tool server a stage is given.

    Named individually rather than swept off the pipeline, so a server added
    later is a deliberate addition here rather than silently covered.
    """
    scope = RunGlossary(path=Path("glossary.json"))
    groups = {
        "glossary": build_glossary_server(scope),
        "notes": build_note_server("write", []),
        "outputs": build_output_server("outputs", make_glossary_tools(scope)),
    }
    return [
        (name, server)
        for name, group in groups.items()
        for server in group.servers.values()
        if isinstance(server, LupMcpServerConfig)
    ]


class TestEveryStageToolServerIsHostedInProcess:
    """The fact that decides whether a second provider is config or a port."""

    def test_stage_tools_are_in_process_servers(self) -> None:
        found = stage_servers()

        assert found, "no stage tool server was found to check"
        for name, server in found:
            assert isinstance(server, LupMcpServerConfig), name

    def test_codex_cannot_serve_an_in_process_server(self) -> None:
        """Not a limitation to work around — the reason this is a port.

        Codex launches each tool group as a subprocess, so a live ``Server``
        instance has nothing it can do with. A per-stage runtime switch
        shipped without the port would raise here for every stage that has
        tools, which is the writer, every reviewer, and the planner.
        """
        for name, server in stage_servers():
            with pytest.raises(ValueError, match="subprocess"):
                codex_mcp_server(name, server)  # pyright: ignore[reportArgumentType]

    def test_a_subprocess_server_is_what_it_would_take(self) -> None:
        """The shape the port has to produce, so the target is unambiguous."""
        rendered = codex_mcp_server(
            "glossary",
            {"command": "inkwell-tools", "args": ["glossary"], "env": {}},
        )

        assert rendered.command == "inkwell-tools"
        assert rendered.args == ["glossary"]


class TestStageToolsWriteIntoTheRunningPipeline:
    """Why the port is architecture rather than a new entry point.

    The servers are not in-process by accident and not merely by convention:
    a stage tool's handler mutates an object the pipeline is holding, and goes
    on holding after the stage ends. A subprocess has no way to reach it, so
    porting means replacing in-memory collection with a protocol — for every
    stage output, not only for the ones a second provider would run.
    """

    async def test_a_note_tool_appends_to_a_list_its_caller_keeps(self) -> None:
        collected: list[AuthorNote] = []
        tool = make_note_tool(collected, "write")

        await tool.handler({"note": "Is this figure current?"})

        assert [held.note for held in collected] == ["Is this figure current?"]
        assert collected[0].stage == "write"
