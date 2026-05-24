"""Interactive writing session — pipeline with live terminal + Google Doc feedback.

The pipeline runs stages sequentially. Between stages, the listener
checks Google Doc comments and drains any terminal input. Both are
passed as context to the next stage.

After the pipeline completes, enters a revision loop where the user
can request changes via the terminal.

Usage:
    await chat_session()                          # open chat
    await chat_session(initial_task="write ...")   # pre-seeded with source
"""

import asyncio
import logging
from datetime import datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.panel import Panel

from lup.metrics import log_metrics_summary, reset_metrics
from lup.paths import project_root

from inkwell.agent.core import run_batch
from inkwell.agent.models import WritingOutput
from inkwell.agent.pipeline import PipelineError, PipelineListener
from inkwell.agent.session import WritingSessionState
from inkwell.agent.tools.google_docs import do_insert_comment

logger = logging.getLogger(__name__)


STAGE_LABELS: dict[str, str] = {
    "extract": "📄 Extracting source material",
    "voice": "🎙️ Analyzing author's voice",
    "plan": "🗺️ Planning article structure",
    "assumptions": "❓ Surfacing questions for author",
    "research": "🔍 Researching claims",
    "write": "✍️ Writing sections",
    "merge": "🧩 Merging into draft",
    "review": "🔎 Reviewing draft",
    "rewrite": "✨ Final rewrite",
    "format": "📐 Formatting output",
    "restart": "🔁 Replanning from author feedback",
    "revise": "🔄 Revising",
}


class InteractiveListener(PipelineListener):
    """Pipeline listener with Rich terminal output and terminal input.

    Displays stage transitions and progress in the terminal.
    Collects feedback from both Google Doc comments and terminal input.
    """

    def __init__(
        self,
        console: Console,
        input_queue: asyncio.Queue[str],
    ) -> None:
        self.console = console
        self.input_queue = input_queue
        self.current_stage = ""

    async def on_stage(self, stage: str, description: str) -> None:
        self.current_stage = stage
        label = STAGE_LABELS.get(stage, stage)
        self.console.print(
            f"\n  [bold blue]{label}[/bold blue]  [dim]{description}[/dim]"
        )

    async def on_progress(self, message: str) -> None:
        self.console.print(f"  💭 [dim]{message}[/dim]")

    async def on_complete(self, output: WritingOutput) -> None:
        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{output.title}[/bold]\n\n"
                f"[dim]Doc:[/dim]    {output.google_doc_url}\n"
                f"[dim]Words:[/dim]  {output.word_count}\n"
                f"[dim]Review:[/dim] {len(output.review_findings)} findings",
                title="Pipeline complete",
                border_style="green",
                width=72,
            )
        )

    async def on_message(self, source: str, message: str) -> None:
        self.console.print(f"  💬 [dim]{message}[/dim]")

    async def collect_feedback(self, state: WritingSessionState) -> list[str]:
        feedback = await super().collect_feedback(state)

        for item in feedback:
            if item.startswith("Author reply"):
                self.console.print(f"  [cyan][GDoc Reply] {item}[/cyan]")
            else:
                self.console.print(f"  [cyan][GDoc Comment] {item}[/cyan]")

        terminal_items: list[str] = []
        while not self.input_queue.empty():
            try:
                msg = self.input_queue.get_nowait()
                terminal_items.append(msg)
            except asyncio.QueueEmpty:
                break

        for msg in terminal_items:
            feedback.append(f"[Terminal] Author direction: {msg}")
            if state.doc_id:
                try:
                    await do_insert_comment(
                        state.doc_id,
                        f"[TERMINAL] {msg}",
                        session_state=state,
                    )
                except (RuntimeError, OSError):
                    logger.warning("Failed to mirror terminal input to GDoc")

        if feedback:
            self.console.print(
                f"  [yellow]Incorporating {len(feedback)} feedback item(s)[/yellow]"
            )

        return feedback

    async def collect_revision(self, state: WritingSessionState) -> str | None:
        revisions: list[str] = []
        while not self.input_queue.empty():
            try:
                revisions.append(self.input_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if revisions:
            return " | ".join(revisions)

        self.console.print(
            "\n  [dim]Type revision instructions, or /done to finish:[/dim]"
        )
        try:
            msg = await asyncio.wait_for(self.input_queue.get(), timeout=300)
        except asyncio.TimeoutError:
            return None
        stripped = msg.strip()
        if stripped in ("/done", "/quit", "/exit", "/q"):
            return None
        return stripped or None


def build_prompt_session() -> PromptSession[str]:
    history_dir = project_root() / ".lup"
    history_dir.mkdir(parents=True, exist_ok=True)

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def newline_binding(event: object) -> None:
        from prompt_toolkit.key_binding import KeyPressEvent

        assert isinstance(event, KeyPressEvent)
        event.current_buffer.newline()

    @kb.add("enter")
    def submit_binding(event: object) -> None:
        from prompt_toolkit.key_binding import KeyPressEvent

        assert isinstance(event, KeyPressEvent)
        event.current_buffer.validate_and_handle()

    return PromptSession(
        message=FormattedText([("class:prompt", "> ")]),
        style=PTStyle.from_dict(
            {
                "prompt": "fg:ansiblue bold",
                "prompt-continuation": "fg:ansiblue",
            }
        ),
        history=FileHistory(str(history_dir / "chat_history")),
        key_bindings=kb,
        multiline=True,
        prompt_continuation=FormattedText([("class:prompt-continuation", "  ")]),
    )


def show_welcome(console: Console) -> None:
    lines = [
        "[bold]Inkwell[/bold] — AI writing agent",
        "",
        "Paste a Claude share link, URL, or writing brief.",
        "The pipeline runs through Google Docs.",
        "Type feedback at any time — picked up between stages.",
        "",
        "[dim]/status  pipeline stage    /doc   open Google Doc[/dim]",
        "[dim]/style   manage corpus     /help  show commands[/dim]",
        "[dim]/done    finish revisions  /quit  exit[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(lines), border_style="blue", width=72))
    console.print()


def show_help(console: Console) -> None:
    console.print(
        "\n  [bold]Commands:[/bold]\n"
        "  /status   Show current pipeline stage\n"
        "  /doc      Show Google Doc URL\n"
        "  /style    Show style corpus entries\n"
        "  /done     Finish revision loop\n"
        "  /quit     Exit session\n"
        "  /help     Show this help\n"
    )


async def read_terminal_input(
    pt_session: PromptSession[str],
    input_queue: asyncio.Queue[str],
    console: Console,
    listener: InteractiveListener | None = None,
) -> None:
    """Background task: read terminal input and push to the queue.

    Slash commands that produce output are handled immediately (not queued).
    Regular text is queued for the next stage boundary or revision prompt.
    """
    while True:
        try:
            user_input = await pt_session.prompt_async()
        except (EOFError, asyncio.CancelledError):
            return
        except KeyboardInterrupt:
            return

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped in ("/quit", "/exit", "/q"):
            input_queue.put_nowait(stripped)
            return

        if stripped == "/help":
            show_help(console)
            continue
        if stripped == "/status":
            stage = listener.current_stage if listener else "unknown"
            console.print(f"  [dim]Stage: {stage}[/dim]")
            continue
        if stripped == "/doc":
            from inkwell.agent.tools.google_docs import get_session_state

            state = get_session_state()
            url = state.doc_url if state else ""
            if url:
                console.print(f"  [dim]{url}[/dim]")
            else:
                console.print("  [dim]No Google Doc created yet.[/dim]")
            continue
        if stripped.startswith("/style"):
            from inkwell.agent.tools.voice import list_style_references

            entries = list_style_references()
            if not entries:
                console.print("  [dim]No style corpus. Use `inkwell style add`.[/dim]")
            else:
                for e in entries:
                    console.print(f"  [dim][{e.kind}] {e.name}[/dim]")
            continue
        if stripped.startswith("/") and stripped not in ("/done",):
            console.print(f"  [red]Unknown command: {stripped}[/red]")
            continue

        input_queue.put_nowait(stripped)
        console.print(f"  [dim]Queued: {stripped[:60]}[/dim]")


async def chat_session(
    *,
    initial_task: str | None = None,
    session_id: str | None = None,
    target_format: str = "lesswrong",
    refs: list[str] | None = None,
    existing_doc_id: str | None = None,
    verbose: bool = False,
) -> None:
    """Run the writing pipeline interactively.

    The pipeline runs the same stages as batch mode. The terminal
    shows progress and accepts steering input. The Google Doc is the
    primary collaboration surface.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    console = Console(highlight=False)
    show_welcome(console)

    reset_metrics()

    pt_session = build_prompt_session()
    input_queue: asyncio.Queue[str] = asyncio.Queue()

    source = initial_task
    if source is None:
        try:
            source = await pt_session.prompt_async()
        except (EOFError, KeyboardInterrupt):
            return

        source = source.strip()
        if not source or source in ("/quit", "/exit", "/q"):
            return

    console.print(
        f"  [dim]Source: {source[:80]}...[/dim]"
        if len(source) > 80
        else f"  [dim]Source: {source}[/dim]"
    )

    listener = InteractiveListener(console, input_queue)

    input_task = asyncio.create_task(
        read_terminal_input(pt_session, input_queue, console, listener)
    )

    try:
        result = await run_batch(
            source,
            target_format=target_format,
            existing_doc_id=existing_doc_id,
            session_id=session_id,
            refs=refs,
            listener=listener,
        )

        if result.cost_usd is not None:
            console.print(f"  [dim]Cost: ${result.cost_usd:.2f}[/dim]")

    except PipelineError as exc:
        console.print(f"\n  [red]Pipeline error: {exc}[/red]")
    except KeyboardInterrupt:
        console.print("\n  [dim]Interrupted.[/dim]")
    except (RuntimeError, OSError, ConnectionError) as exc:
        console.print(f"\n  [red]Error: {exc}[/red]")
    finally:
        input_task.cancel()
        try:
            await input_task
        except asyncio.CancelledError:
            pass

    log_metrics_summary()
    console.print("\n[dim]Session ended.[/dim]")
