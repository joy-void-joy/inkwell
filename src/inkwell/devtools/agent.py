"""Agent introspection and interactive debugging tools.

Commands:
- inspect: Pretty-print the full agent configuration (tools, schemas, prompt, subagents)
- serve-tools: Start SDK tools as an MCP stdio server (used by ``chat``)
- chat: Launch an interactive ``claude`` session with the agent's tools and prompt
- repl: Interactive REPL with the agent via the SDK (continuous session)
- reader-feedback: Ingest a reader-feedback export and report what it routed

Examples::

    $ uv run lup-devtools agent inspect
    $ uv run lup-devtools agent inspect --json
    $ uv run lup-devtools agent inspect --full
    $ uv run lup-devtools agent chat
    $ uv run lup-devtools agent chat --model opus --no-tools
    $ uv run lup-devtools agent repl
    $ uv run lup-devtools agent repl --model sonnet --no-prompt
    $ uv run lup-devtools agent serve-tools
    $ uv run lup-devtools agent reader-feedback export.json --out notes/reader
"""

import asyncio
import hashlib
import inspect as inspect_mod
import io
import json
import logging
import signal
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from prompt_toolkit.key_binding import KeyPressEvent
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from lup.types import JsonObject

if TYPE_CHECKING:
    from rich.console import Console

    from lup.runtime.contracts import Session
    from lup.runtime.models import TurnResult

import sh
import typer

from inkwell.agent.config import settings
from inkwell.agent.models import WritingOutput
from inkwell.agent.stages import (
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
    PLANNER_SYSTEM,
    MERGE_PROMPT,
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    SECTION_WRITER_PROMPT,
    STYLE_REVIEWER_PROMPT,
)
from inkwell.agent.pipeline import build_research_tools
from inkwell.agent.reader_feedback import (
    ReaderFeedbackTree,
    SubstantiveRule,
    ingest_reader_feedback,
)
from inkwell.agent.tools.extract import EXTRACT_TOOLS
from inkwell.agent.tools.google_docs import GOOGLE_DOCS_TOOLS
from inkwell.agent.tools.research.fetch import FETCH_TOOLS
from inkwell.agent.tools.voice import VOICE_TOOLS
from inkwell.agent.client import provider_factory
from lup.mcp import LupMcpTool, create_mcp_server, serve_stdio
from lup.runtime.models import turn_request
from lup.telemetry.metrics import configure_metrics, metrics_path
from lup.workspace.context import SessionContext, read_session_context
from lup.workspace.notes import session_gate_flag, setup_notes

logger = logging.getLogger(__name__)

REPL_SYSTEM_PROMPT = """\
You are a development REPL for Inkwell, an AI writing agent. You have
the pipeline's source, research, docs, and compute tools available for
manual testing. Use them directly when asked — this session is for
exercising tools and inspecting their behavior, not for running the
full writing pipeline."""


class StagePrompt(BaseModel):
    """One pipeline stage's system prompt, as the inspector reports it."""

    name: str = Field(description="Stage name, as the inspector labels it")
    prompt: str = Field(description="The system prompt that stage runs under")


STAGE_PROMPTS: tuple[StagePrompt, ...] = (
    StagePrompt(name="planner", prompt=PLANNER_SYSTEM),
    StagePrompt(name="researcher", prompt=RESEARCHER_PROMPT),
    StagePrompt(name="section_writer", prompt=SECTION_WRITER_PROMPT),
    StagePrompt(name="merge", prompt=MERGE_PROMPT),
    StagePrompt(name="narrative_reviewer", prompt=NARRATIVE_REVIEWER_PROMPT),
    StagePrompt(name="fact_checker", prompt=FACT_CHECKER_PROMPT),
    StagePrompt(name="style_reviewer", prompt=STYLE_REVIEWER_PROMPT),
    StagePrompt(name="rewriter", prompt=REWRITER_SYSTEM),
)


class ImageKind(BaseModel):
    """One image type this REPL handles: how it is named, and how it is saved."""

    media_type: str = Field(description="MIME type, as the clipboard names it")
    suffix: str = Field(description="File extension the image is saved under")
    from_clipboard: bool = Field(
        default=True,
        description="Whether to offer this type when reading the clipboard",
    )


IMAGE_KINDS: tuple[ImageKind, ...] = (
    ImageKind(media_type="image/png", suffix=".png"),
    ImageKind(media_type="image/jpeg", suffix=".jpg"),
    ImageKind(media_type="image/webp", suffix=".webp"),
    ImageKind(media_type="image/gif", suffix=".gif", from_clipboard=False),
)


class ClipboardImage(BaseModel):
    """One image pasted from the clipboard, and what the clipboard called it."""

    media_type: str = Field(description="MIME type the clipboard offered it under")
    data: bytes = Field(description="The image's raw bytes")

    @property
    def suffix(self) -> str:
        """The extension this image saves under, generic if unrecognized."""
        return next(
            (kind.suffix for kind in IMAGE_KINDS if kind.media_type == self.media_type),
            ".bin",
        )

    def save_to(self, images_dir: Path) -> Path:
        """Write the image under its content hash, keeping a copy already there."""
        path = images_dir / (hashlib.sha256(self.data).hexdigest()[:12] + self.suffix)
        if not path.exists():
            path.write_bytes(self.data)
        return path


def save_images(images: list[ClipboardImage], images_dir: Path) -> list[Path]:
    """Save raw image data to disk, deduplicating by content hash."""
    images_dir.mkdir(parents=True, exist_ok=True)
    return [image.save_to(images_dir) for image in images]


app = typer.Typer(no_args_is_help=True)


def xclip() -> sh.Command:
    """The xclip binary, looked up on use rather than at import.

    Both readers below already treat its absence as "no clipboard", but a
    module-level lookup raises before either can: importing anything from this
    module failed outright on a machine without xclip, which took the whole
    CLI — and CI — down with it.
    """
    return sh.Command("xclip")


def read_clipboard_image() -> ClipboardImage | None:
    """Read image data from the system clipboard via xclip.

    Returns the first offered type this REPL handles, or None when the
    clipboard holds no image it can read.
    """
    try:
        targets = str(xclip()("-selection", "clipboard", "-o", "-t", "TARGETS"))
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return None

    for kind in IMAGE_KINDS:
        if not kind.from_clipboard or kind.media_type not in targets:
            continue
        try:
            buf = io.BytesIO()
            xclip()("-selection", "clipboard", "-o", "-t", kind.media_type, _out=buf)
            data = buf.getvalue()
            if data:
                return ClipboardImage(media_type=kind.media_type, data=data)
        except sh.ErrorReturnCode:
            continue
    return None


def read_clipboard_text() -> str | None:
    """Read text from the system clipboard via xclip."""
    try:
        text = str(xclip()("-selection", "clipboard", "-o"))
        return text if text else None
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return None


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def print_model_source(
    out: io.StringIO, model: type, label: str, indent: str = "    "
) -> None:
    """Print the Python source of a BaseModel class."""
    out.write(f"\n{indent}{label}:\n")
    try:
        source = inspect_mod.getsource(model)
        for line in source.splitlines():
            out.write(f"{indent}  {line}\n")
    except (OSError, TypeError):
        out.write(f"{indent}  {model.__name__} (source unavailable)\n")


def tool_location(tool: LupMcpTool) -> str:
    """Get file:line for the tool handler (unwraps decorators)."""
    handler = inspect_mod.unwrap(tool.handler)
    try:
        filename = Path(inspect_mod.getfile(handler)).name
        _, lineno = inspect_mod.getsourcelines(handler)
        return f"{filename}:{lineno}"
    except (OSError, TypeError):
        return "?"


def tool_signature(tool: LupMcpTool) -> str:
    """One-liner: input fields → output model name, file:line."""

    def field_label(name: str, field: FieldInfo) -> str:
        """One input field as the signature spells it: name, and type if known."""
        annotation = field.annotation
        type_name = getattr(annotation, "__name__", None) if annotation else None
        return f"{name}: {type_name}" if type_name else name

    fields = ", ".join(
        field_label(name, field)
        for name, field in tool.input_model.model_fields.items()
    )
    output_part = f" → {tool.output_model.__name__}" if tool.output_model else ""
    return f"({fields}){output_part}  [{tool_location(tool)}]"


def print_tool_compact(out: io.StringIO, tool: LupMcpTool) -> None:
    """Print a single tool as a one-liner."""
    out.write(f"    {tool.name}{tool_signature(tool)}\n")


def print_tool_full(out: io.StringIO, tool: LupMcpTool) -> None:
    """Print a single tool with full description and schemas."""
    out.write(f"\n  {tool.name}\n")
    out.write(f"  {'─' * len(tool.name)}\n")

    for line in textwrap.wrap(tool.description, width=72):
        out.write(f"    {line}\n")

    print_model_source(out, tool.input_model, "Input")

    if tool.output_model is not None:
        print_model_source(out, tool.output_model, "Output")


def collect_tools_by_server() -> dict[str, list[LupMcpTool]]:
    """Collect LupMcpTool instances grouped by live pipeline server name."""
    return {
        "source": [*FETCH_TOOLS, *EXTRACT_TOOLS],
        "research": build_research_tools(),
        "docs": [*GOOGLE_DOCS_TOOLS, *VOICE_TOOLS],
    }


def collect_all_tools() -> list[LupMcpTool]:
    """Collect all LupMcpTool instances from known tool modules."""
    return [
        tool
        for server_tools in collect_tools_by_server().values()
        for tool in server_tools
    ]


def tool_to_dict(t: LupMcpTool) -> JsonObject:
    """Serialize a LupMcpTool for JSON output."""
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_model.model_json_schema(),
        "output_schema": t.output_model.model_json_schema() if t.output_model else None,
    }


def page_output(text: str) -> None:
    """Write text through a pager (less) if stdout is a tty, otherwise print."""
    if not sys.stdout.isatty():
        sys.stdout.write(text)
        return
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        tmp.write(text)
        tmp.close()
        less = sh.Command("less")
        less("-R", "-F", "-X", tmp.name, _fg=True)
    except (sh.CommandNotFound, sh.ErrorReturnCode):
        sys.stdout.write(text)
    finally:
        Path(tmp.name).unlink()


@app.command("inspect")
def inspect_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as machine-readable JSON"),
    ] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="Show full details (tool schemas, full prompt)"),
    ] = False,
) -> None:
    """Inspect the full agent configuration: tools, schemas, prompt, agents."""
    tools_by_server = collect_tools_by_server()
    all_tools = collect_all_tools()
    stages = STAGE_PROMPTS

    if as_json:
        data: JsonObject = {
            "model": settings.model,
            "max_thinking_tokens": settings.max_thinking_tokens,
            "tools": [tool_to_dict(t) for t in all_tools],
            "output_schema": WritingOutput.model_json_schema(),
            "pipeline_stages": {stage.name: stage.prompt for stage in stages}
            if full
            else {stage.name: {"prompt_length": len(stage.prompt)} for stage in stages},
        }
        typer.echo(json.dumps(data, indent=2))
        return

    # --- Pretty-print mode (write to buffer, then page) ---
    out = io.StringIO()

    out.write("=" * 60 + "\n")
    out.write("  Agent Configuration\n")
    out.write("=" * 60 + "\n")

    # Model
    out.write(f"\nModel: {settings.model}\n")
    out.write(f"Max thinking tokens: {settings.max_thinking_tokens}\n")

    # Tools grouped by server
    total_tools = sum(len(ts) for ts in tools_by_server.values())
    out.write(f"\n{'─' * 60}\n")
    out.write(f"  MCP Tools ({total_tools})\n")
    out.write(f"{'─' * 60}\n")
    for server_name, server_tools in tools_by_server.items():
        out.write(f"\n  {server_name} ({len(server_tools)} tools)\n")
        for t in server_tools:
            if full:
                print_tool_full(out, t)
            else:
                print_tool_compact(out, t)

    # Agent output schema
    out.write(f"\n{'─' * 60}\n")
    out.write("  Agent Output Schema\n")
    out.write(f"{'─' * 60}\n")
    if full:
        print_model_source(out, WritingOutput, "WritingOutput", indent="  ")
    else:
        for name, f in WritingOutput.model_fields.items():
            ann = f.annotation
            type_name = getattr(ann, "__name__", None) if ann is not None else None
            out.write(f"    {name}: {type_name or '?'}\n")

    # Pipeline stage prompts — the agent's actual system prompts
    out.write(f"\n{'─' * 60}\n")
    out.write(f"  Pipeline Stage Prompts ({len(stages)})\n")
    out.write(f"{'─' * 60}\n")
    for stage in stages:
        out.write(f"\n  {stage.name} ({len(stage.prompt)} chars)\n")
        if full:
            for line in stage.prompt.splitlines():
                out.write(f"    {line}\n")

    if not full:
        out.write("\n  (use --full to see complete stage prompts)\n")

    out.write("\n")

    page_output(out.getvalue())


# ---------------------------------------------------------------------------
# serve-tools
# ---------------------------------------------------------------------------


def harness_session_context(name: str) -> SessionContext:
    """Open the session a natively launched tool server serves.

    An adapter-launched server is handed a session that already exists; a
    server a native runtime starts is the first thing in that session to run,
    so it opens one under the name it was given. The name is the whole
    identity, and every group of one runtime's session is started with the
    same name, so those processes agree on where session state lives without
    a channel between them.
    """
    notes = setup_notes(session_id=name, task_id=name, type="harness")
    return SessionContext(
        session_dir=notes.session,
        outputs_dir=notes.output.parent,
        gate_flag=session_gate_flag(name),
        session_id=name,
        task_id=name,
    )


@app.command("serve-tools")
def serve_tools_cmd(
    server: Annotated[
        str | None,
        typer.Option("--server", help="Serve one tool group rather than all of them"),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Session the served tools read and write under"),
    ] = None,
) -> None:
    """Start SDK tools as an MCP stdio server.

    A natively launched harness runs one of these per tool group, all under
    one session name, so the tools of one native session write where the next
    one will find them. The ``chat`` command launches it the same way,
    ungrouped.
    """
    context = read_session_context()
    if context is None and session is not None:
        context = harness_session_context(session)
    if context is not None:
        configure_metrics(metrics_path(context.session_dir))

    groups = collect_tools_by_server()
    tools = (
        collect_all_tools()
        if server is None
        else groups.get(server)  # lup: ignore[dict-get] — group map
    )
    if tools is None:
        typer.echo(f"--server must be one of: {', '.join(groups)}", err=True)
        raise typer.Exit(2)

    # The config's server already answers list_tools and call_tool for these,
    # so serving them over stdio is the whole of what is left to do.
    serve_stdio(create_mcp_server(server or "lup-tools", tools=tools))


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


@app.command("chat")
def chat_cmd(
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Override the model (e.g. sonnet, opus)"),
    ] = None,
    no_tools: Annotated[
        bool,
        typer.Option("--no-tools", help="Skip MCP tool server"),
    ] = False,
    no_prompt: Annotated[
        bool,
        typer.Option("--no-prompt", help="Skip appending the agent system prompt"),
    ] = False,
) -> None:
    """Launch an interactive claude session with the agent's tools and prompt.

    Starts the SDK MCP tools as a stdio server, generates the system prompt,
    and execs into ``claude`` with the right flags.
    """
    claude_args: list[str] = []

    # Model
    effective_model = model or settings.model
    claude_args.extend(["--model", effective_model])

    # System prompt
    if not no_prompt:
        claude_args.extend(["--append-system-prompt", REPL_SYSTEM_PROMPT])

    # MCP config with serve-tools as stdio server
    mcp_config_path: str | None = None
    if not no_tools:
        mcp_config = {
            "mcpServers": {
                "lup-tools": {
                    "command": "uv",
                    "args": ["run", "lup-devtools", "agent", "serve-tools"],
                }
            }
        }
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="lup-mcp-", delete=False
        )
        json.dump(mcp_config, tmp)
        tmp.close()
        mcp_config_path = tmp.name
        claude_args.extend(["--mcp-config", mcp_config_path])

    typer.echo(f"Launching claude with model={effective_model}")
    if not no_tools:
        typer.echo(f"MCP config: {mcp_config_path}")
    if not no_prompt:
        typer.echo("System prompt: appended")

    # exec into claude so the user gets a full interactive session
    try:
        claude = sh.Command("claude")
        claude(*claude_args, _fg=True)
    except sh.CommandNotFound:
        typer.echo(
            "Error: 'claude' CLI not found. Install Claude Code first.", err=True
        )
        raise typer.Exit(1)
    except sh.ErrorReturnCode:
        pass  # claude exited normally or user quit
    finally:
        if mcp_config_path:
            try:
                Path(mcp_config_path).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# repl
# ---------------------------------------------------------------------------


class Interrupted(Exception):
    """Raised when the user interrupts response collection via Ctrl-C."""


async def send_interruptible(
    session: "Session", prompt: str, console: "Console"
) -> "TurnResult[None]":
    """Run one turn with Ctrl-C interrupt support.

    First Ctrl-C asks the turn to stop (graceful). Second cancels the task
    waiting on it (force).
    """
    loop = asyncio.get_running_loop()
    interrupt_count = 0

    turn = await session.start(turn_request(prompt))
    send_task = asyncio.create_task(turn.turn.result())

    def on_sigint() -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1 and turn.interrupt is not None:
            console.print("\n  [dim]interrupting...[/dim]")
            asyncio.ensure_future(turn.interrupt.interrupt())
        else:
            send_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)
    try:
        return await send_task
    except asyncio.CancelledError:
        raise Interrupted from None
    finally:
        loop.remove_signal_handler(signal.SIGINT)


async def repl(
    *,
    model: str | None = None,
    no_tools: bool = False,
    no_prompt: bool = False,
) -> None:
    """Run the interactive REPL loop."""
    from contextlib import AsyncExitStack

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style as PTStyle
    from rich.console import Console
    from rich.panel import Panel

    from inkwell.agent.pipeline import (
        build_compute_server,
        build_research_servers,
        build_source_server,
    )
    from lup.workspace.content_safety import configure
    from lup.mcp import McpServerEntry, create_mcp_server
    from lup.workspace.paths import project_root
    from lup.sandbox.container import Sandbox

    console = Console(highlight=False)
    effective_model = model or settings.model

    mcp_servers: dict[str, McpServerEntry] = {}
    stack = AsyncExitStack()

    if not no_tools:
        repl_dir = project_root() / ".lup" / "repl"
        configure(directory=repl_dir / "content")
        sandbox = Sandbox(
            session_id="repl",
            shared_dir=repl_dir / "sandbox_shared",
            timeout_seconds=settings.sandbox_timeout_seconds,
        )
        stack.enter_context(sandbox)

        mcp_servers = {
            **build_source_server().servers,
            **build_research_servers(),
            **build_compute_server(sandbox, repl_dir / "artifacts").servers,
            "docs": create_mcp_server(
                name="docs", tools=[*GOOGLE_DOCS_TOOLS, *VOICE_TOOLS]
            ),
        }

        # Shutdown message — registered last so it runs first (LIFO)
        stack.callback(lambda: console.print("[dim]Shutting down...[/dim]"))

    prompt = REPL_SYSTEM_PROMPT if not no_prompt else ""

    # Welcome panel with server → tool listing
    panel_lines = [
        "[bold]✻ Agent REPL[/bold]",
        f"[dim]model:[/dim] {effective_model}",
    ]
    if not no_tools:
        servers = collect_tools_by_server()
        for i, (name, tools) in enumerate(servers.items()):
            is_last_server = i == len(servers) - 1
            panel_lines.append(f"[dim]{'└' if is_last_server else '├'} {name}[/dim]")
            for j, t in enumerate(tools):
                is_last_tool = j == len(tools) - 1
                branch = "  └" if is_last_tool else "  ├"
                if not is_last_server:
                    branch = f"[dim]│[/dim] {'└' if is_last_tool else '├'}"
                panel_lines.append(f"[dim]{branch}[/dim] {t.name}")
    else:
        panel_lines.append("[dim]no tools[/dim]")
    panel_lines += [
        "",
        "[dim]/quit · Ctrl-C stop · Ctrl-V paste image · Alt+Enter newline[/dim]",
    ]

    console.print()
    console.print(Panel("\n".join(panel_lines), border_style="blue", width=60))
    console.print()

    # -- prompt_toolkit session --
    session_cost = 0.0
    pending_images: list[ClipboardImage] = []

    def rprompt() -> FormattedText:
        parts = [effective_model]
        if pending_images:
            n = len(pending_images)
            parts.append(f"{n} img{'s' if n > 1 else ''}")
        if session_cost:
            parts.append(f"${session_cost:.4f}")
        return FormattedText([("class:rprompt", " · ".join(parts))])

    history_dir = project_root() / ".lup"
    history_dir.mkdir(parents=True, exist_ok=True)

    # Key bindings: Enter submits, Alt+Enter inserts newline
    kb = KeyBindings()

    @kb.add("escape", "enter")  # Alt+Enter or Esc then Enter
    def newline_binding(event: KeyPressEvent) -> None:
        event.current_buffer.newline()

    @kb.add("enter")
    def submit_binding(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("c-v")
    def paste_binding(event: KeyPressEvent) -> None:
        result = read_clipboard_image()
        if result is not None:
            pending_images.append(result)
            n = len(pending_images)
            console.print(
                f"[dim]{n} image{'s' if n > 1 else ''} attached (/drop to clear)[/dim]"
            )
        else:
            text = read_clipboard_text()
            if text:
                event.current_buffer.insert_text(text)

    pt_session: PromptSession[str] = PromptSession(
        message=FormattedText([("class:prompt", "❯ ")]),
        rprompt=rprompt,
        style=PTStyle.from_dict(
            {
                "prompt": "fg:ansiblue bold",
                "prompt-continuation": "fg:ansiblue",
                "rprompt": "fg:#666666",
            }
        ),
        history=FileHistory(str(history_dir / "repl_history")),
        completer=WordCompleter(
            ["/quit", "/exit", "/q", "/help", "/drop"],
            sentence=True,
        ),
        key_bindings=kb,
        multiline=True,
        prompt_continuation=FormattedText([("class:prompt-continuation", "··· ")]),
    )

    factory = provider_factory(
        model=effective_model,
        system_prompt=prompt,
        max_thinking_tokens=settings.max_thinking_tokens or (128_000 - 1),
        autonomy="unattended",
        tool_servers=mcp_servers or None,
    )
    try:
        async with stack:
            async with factory.open() as opened:
                last_input_sigint = 0.0

                while True:
                    try:
                        user_input = await pt_session.prompt_async()
                    except (EOFError, asyncio.CancelledError):
                        console.print()
                        break
                    except KeyboardInterrupt:
                        now = time.monotonic()
                        if now - last_input_sigint < 2.0:
                            console.print()
                            break
                        last_input_sigint = now
                        console.print("[dim]Press Ctrl-C again to exit[/dim]")
                        continue

                    last_input_sigint = 0.0
                    stripped = user_input.strip()
                    if not stripped:
                        continue
                    if stripped in ("/quit", "/exit", "/q"):
                        break
                    if stripped == "/drop":
                        pending_images.clear()
                        console.print("[dim]images cleared[/dim]")
                        continue

                    console.print("[dim]thinking...[/dim]")
                    if pending_images:
                        images_dir = project_root() / ".lup" / "images"
                        saved = save_images(pending_images, images_dir)
                        path_list = ", ".join(str(p) for p in saved)
                        query_text = (stripped + "\n\n" if stripped else "") + (
                            f"[image attached: {path_list}]"
                        )
                        pending_images.clear()
                    else:
                        query_text = user_input
                    try:
                        result = await send_interruptible(
                            opened.session, query_text, console
                        )
                        parts: list[str] = []
                        seconds = result.duration.total_seconds()
                        if seconds:
                            parts.append(f"{seconds:.1f}s")
                        if result.usage.cost_usd:
                            session_cost += result.usage.cost_usd
                            parts.append(f"${result.usage.cost_usd:.4f}")
                        if parts:
                            console.print(f"  [dim]{' · '.join(parts)}[/dim]")
                        console.print()
                    except Interrupted:
                        console.print("  [dim]interrupted[/dim]\n")
                    except RuntimeError as e:
                        console.print(f"  [red]error:[/red] {e}\n")
    except KeyboardInterrupt:
        # Additional Ctrl+C during cleanup — containers will be cleaned
        # on next start via stale container removal
        pass


@app.command("reader-feedback")
def reader_feedback_cmd(
    source: Annotated[
        Path,
        typer.Argument(help="Reader-feedback export file, or a directory of them"),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Where to file the per-section notes"),
    ] = Path("notes/reader"),
    min_length: Annotated[
        int,
        typer.Option("--min-length", help="Shortest prose that counts as substantive"),
    ] = SubstantiveRule().min_length,
    prose_field: Annotated[
        list[str] | None,
        typer.Option("--prose-field", help="Field whose prose counts (repeatable)"),
    ] = None,
) -> None:
    """Ingest a reader-feedback export into per-section notes and report the split.

    The same ingestion a run performs at session start, so an author can see
    what an export would route — and where — before spending a pipeline on it.
    """
    rule = SubstantiveRule(
        prose_fields=prose_field or SubstantiveRule().prose_fields,
        min_length=min_length,
    )
    report = ingest_reader_feedback(
        ReaderFeedbackTree(root=out.expanduser()), source.expanduser(), rule=rule
    )
    typer.echo(report.summary())
    for row in report.malformed:
        typer.echo(f"  row {row.position}: {row.error}")
    typer.echo(f"Filed under {out}")


@app.command("repl")
def repl_cmd(
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Override the model"),
    ] = None,
    no_tools: Annotated[
        bool,
        typer.Option("--no-tools", help="Skip MCP tools"),
    ] = False,
    no_prompt: Annotated[
        bool,
        typer.Option("--no-prompt", help="Skip agent system prompt"),
    ] = False,
) -> None:
    """Interactive REPL — continuous session with the agent via the SDK."""
    try:
        asyncio.run(repl(model=model, no_tools=no_tools, no_prompt=no_prompt))
    except KeyboardInterrupt:
        pass
