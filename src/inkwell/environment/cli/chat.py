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
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.panel import Panel

from lup.metrics import log_metrics_summary, reset_metrics
from lup.paths import project_root
from lup.trace import active_agents

from inkwell.agent.core import SessionTrace, run_session
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
    "sync": "🔄 Syncing feedback",
}


SPINNER_FRAMES = "◇◈◆◈"


def build_toolbar(listener: InteractiveListener | None = None):
    """Return a callable for prompt-toolkit's bottom_toolbar.

    Shows a spinner, the current pipeline stage, and active nested agents.
    Refreshed automatically by prompt-toolkit's refresh_interval.
    """
    frame = 0

    def get_toolbar():
        nonlocal frame
        parts: list[tuple[str, str]] = []
        stage = listener.current_stage if listener else ""
        label = STAGE_LABELS.get(stage, "")
        if label or active_agents:
            parts.append(("class:toolbar.spinner", f" {SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]} "))
            frame += 1
        if label:
            parts.append(("class:toolbar.stage", f"{label} "))
        if active_agents:
            agents_str = "  ".join(sorted(active_agents))
            parts.append(("class:toolbar.agents", f" {agents_str} "))
        return parts or None

    return get_toolbar


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
        super().__init__()
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


def build_prompt_session(
    listener: InteractiveListener | None = None,
) -> PromptSession[str]:
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
                "bottom-toolbar": "bg:#1a1a2e",
                "toolbar.spinner": "bg:#1a1a2e #6c8cbf",
                "toolbar.stage": "bg:#1a1a2e #e0e0ff bold",
                "toolbar.agents": "bg:#1a1a2e #88ccff italic",
            }
        ),
        history=FileHistory(str(history_dir / "chat_history")),
        key_bindings=kb,
        multiline=True,
        prompt_continuation=FormattedText([("class:prompt-continuation", "  ")]),
        bottom_toolbar=build_toolbar(listener),
        refresh_interval=0.5,
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
        "[dim]/sync   poll edits+comments  /style manage corpus[/dim]",
        "[dim]/done   finish revisions  /quit  exit  /help  commands[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(lines), border_style="blue", width=72))
    console.print()


def show_help(console: Console) -> None:
    console.print(
        "\n  [bold]Commands:[/bold]\n"
        "  /sync     Poll GDoc comments + tab edits, trigger revision\n"
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
    session_state: WritingSessionState | None = None,
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
            url = session_state.doc_url if session_state else ""
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
        if stripped == "/sync":
            if listener:
                listener.request_sync()
                console.print("  [yellow]Sync requested — polling comments + edits[/yellow]")
            else:
                console.print("  [dim]No active pipeline to sync.[/dim]")
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
    resume_session_id: str | None = None,
    resume_from_stage: str | None = None,
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
        session_id = resume_session_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    console = Console(highlight=False)
    show_welcome(console)

    reset_metrics()

    input_queue: asyncio.Queue[str] = asyncio.Queue()
    listener = InteractiveListener(console, input_queue)
    pt_session = build_prompt_session(listener)

    with patch_stdout(raw=True):
        source = initial_task
        if source is None and resume_session_id is None:
            try:
                source = await pt_session.prompt_async()
            except (EOFError, KeyboardInterrupt):
                return

            source = source.strip()
            if not source or source in ("/quit", "/exit", "/q"):
                return

        if source:
            console.print(
                f"  [dim]Source: {source[:80]}...[/dim]"
                if len(source) > 80
                else f"  [dim]Source: {source}[/dim]"
            )
        elif resume_session_id:
            console.print(f"  [dim]Resuming session: {resume_session_id}[/dim]")

        trace_holder: list[SessionTrace] = []
        session_state = WritingSessionState()

        input_task = asyncio.create_task(
            read_terminal_input(
                pt_session, input_queue, console, listener, session_state
            )
        )

        try:
            result = await run_session(
                source=source,
                resume_session_id=resume_session_id,
                resume_from_stage=resume_from_stage,
                target_format=target_format,
                existing_doc_id=existing_doc_id,
                session_id=session_id,
                refs=refs,
                listener=listener,
                persistent=False,
                trace_holder=trace_holder,
                session_state=session_state,
            )

            if result.cost_usd is not None:
                console.print(f"  [dim]Cost: ${result.cost_usd:.2f}[/dim]")
                stage_costs = result.output.stage_costs if result.output else {}
                if stage_costs:
                    grouped: dict[str, list[float]] = {}
                    for name, data in stage_costs.items():
                        if name.startswith("write:"):
                            group = "write"
                        elif name.startswith("review:"):
                            group = "review"
                        elif name.startswith("patch:"):
                            group = "patch"
                        else:
                            group = name
                        if group not in grouped:
                            grouped[group] = [0.0, 0.0, 0]
                        grouped[group][0] += data.get("cost_usd", 0)
                        grouped[group][1] += data.get("duration_s", 0)
                        grouped[group][2] += data.get("calls", 0)
                    for name, vals in sorted(
                        grouped.items(), key=lambda kv: -kv[1][0]
                    ):
                        cost, dur, calls = vals[0], vals[1], int(vals[2])
                        pct = (cost / result.cost_usd * 100) if result.cost_usd else 0
                        label = f"{name} ({calls})" if calls > 1 else name
                        console.print(
                            f"    [dim]{label:.<30s} "
                            f"${cost:>6.2f}  ({pct:4.1f}%)  "
                            f"{dur:>5.0f}s[/dim]"
                        )

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
            if trace_holder:
                trace_path = trace_holder[0].save()
                console.print(f"\n  [dim]Trace saved to: {trace_path}[/dim]")

    log_metrics_summary()
    console.print("\n[dim]Session ended.[/dim]")
