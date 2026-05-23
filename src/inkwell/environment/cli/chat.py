"""Interactive chat session for the writing agent.

Launches a persistent agent session with concurrent terminal input.
The user can type at any time — a background thread reads stdin and
routes messages to session state. The PreToolUse hook in core.py
surfaces them before the agent's next tool call; if the agent is
sleeping, the scheduler is woken.

Usage:
    await chat_session()                          # open chat
    await chat_session(initial_task="write ...")   # pre-seeded
"""

import asyncio
import logging
import select as sel
import signal
import sys
import threading
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.panel import Panel

from lup.client import ResponseCollector, build_client
from lup.metrics import log_metrics_summary, reset_metrics
from lup.paths import project_root
from lup.trace import print_message

from inkwell.agent.config import settings
from inkwell.agent.core import setup_session
from inkwell.agent.session import WritingSessionState
from lup.realtime import Scheduler

logger = logging.getLogger(__name__)


async def collect_with_stdin(
    collector: ResponseCollector,
    session_state: WritingSessionState,
    scheduler: Scheduler,
    console: Console,
) -> None:
    """Collect agent responses while accepting terminal input concurrently.

    A background thread reads stdin so the user can type at any time —
    not just when the agent sleeps. Input is added to
    session_state.terminal_messages and:
    - During thinking: surfaced via PreToolUse hook before the next tool call
    - During sleep/standby: wakes the scheduler
    """
    loop = asyncio.get_running_loop()
    interrupt_count = 0

    async def do_collect() -> None:
        try:
            async for message in collector:
                print_message(message, trace=collector.trace_logger)
        except (RuntimeError, OSError, ConnectionError) as exc:
            console.print(f"\n  [red]Agent error: {exc}[/red]")
            logger.exception("Agent collector failed")

    collect_task = asyncio.create_task(do_collect())

    def on_sigint() -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            console.print("\n  [dim]interrupting...[/dim]")
            asyncio.ensure_future(collector.client.interrupt())
        else:
            collect_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)

    input_queue: asyncio.Queue[str] = asyncio.Queue()
    stop_reading = threading.Event()

    def read_stdin() -> None:
        while not stop_reading.is_set():
            try:
                ready, _, _ = sel.select([sys.stdin], [], [], 0.5)
                if ready:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    text = line.strip()
                    if text:
                        loop.call_soon_threadsafe(input_queue.put_nowait, text)
            except (EOFError, OSError, ValueError):
                break

    reader_thread = threading.Thread(target=read_stdin, daemon=True)
    reader_thread.start()

    async def process_input() -> None:
        while True:
            try:
                text = await input_queue.get()
            except asyncio.CancelledError:
                break
            if text in ("/quit", "/exit", "/q"):
                collect_task.cancel()
                break
            if handle_slash_command(text, session_state, console):
                continue
            session_state.add_terminal_message(text)
            if scheduler.is_sleeping:
                scheduler.wake("terminal")
            console.print("  [dim]✓ noted[/dim]")

    input_task = asyncio.create_task(process_input())

    try:
        await collect_task
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        stop_reading.set()
        input_task.cancel()
        try:
            await input_task
        except asyncio.CancelledError:
            pass
        reader_thread.join(timeout=1)


def handle_slash_command(
    text: str,
    session_state: WritingSessionState,
    console: Console,
) -> bool:
    """Handle in-chat slash commands. Returns True if handled."""
    match text:
        case "/status":
            show_status(session_state, console)
            return True
        case "/doc":
            if session_state.doc_url:
                console.print(f"  {session_state.doc_url}")
            else:
                console.print("  [dim]No Google Doc created yet[/dim]")
            return True
        case "/help":
            show_help(console)
            return True
        case _ if text.startswith("/style add "):
            add_style_reference(text[11:].strip(), console)
            return True
        case "/style list":
            list_style_corpus(console)
            return True
    return False


def show_status(
    session_state: WritingSessionState,
    console: Console,
) -> None:
    lines = [f"  [bold]Stage:[/bold] {session_state.stage}"]
    if session_state.sections:
        lines.append("  [bold]Sections:[/bold]")
        markers = {"planned": "[ ]", "writing": "[>]", "drafted": "[~]", "reviewed": "[~]", "final": "[x]"}
        for s in session_state.sections:
            marker = markers.get(s["status"], "[ ]")
            lines.append(f"    {marker} {s['title']} — {s['status']}")
    if session_state.pending_questions:
        lines.append(f"  [bold]Pending questions:[/bold] {len(session_state.pending_questions)}")
    console.print("\n".join(lines))


def show_help(console: Console) -> None:
    console.print(
        "  [bold]/status[/bold]          Pipeline progress\n"
        "  [bold]/doc[/bold]             Open Google Doc URL\n"
        "  [bold]/style add[/bold] path  Add a voice reference\n"
        "  [bold]/style list[/bold]      List style corpus\n"
        "  [bold]/quit[/bold]            Exit"
    )


def add_style_reference(source: str, console: Console) -> None:
    style_dir = Path(settings.style_corpus_path)
    style_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source).expanduser()
    if source_path.exists():
        content = source_path.read_text(encoding="utf-8")
        dest = style_dir / source_path.name
        dest.write_text(content, encoding="utf-8")
        console.print(f"  Added {source_path.name} ({len(content)} chars)")
    else:
        refs_file = style_dir / "urls.txt"
        existing = refs_file.read_text(encoding="utf-8") if refs_file.exists() else ""
        if source not in existing:
            with refs_file.open("a", encoding="utf-8") as f:
                f.write(source + "\n")
            console.print(f"  Added URL: {source}")
        else:
            console.print(f"  Already in corpus: {source}")


def list_style_corpus(console: Console) -> None:
    style_dir = Path(settings.style_corpus_path)
    if not style_dir.exists():
        console.print("  [dim]No style corpus. Use /style add <path>[/dim]")
        return

    files = list(style_dir.glob("*.md")) + list(style_dir.glob("*.txt"))
    if not files:
        console.print("  [dim]Style corpus is empty[/dim]")
        return

    for f in sorted(files):
        if f.name == "urls.txt":
            urls = f.read_text(encoding="utf-8").strip().split("\n")
            for url in urls:
                if url.strip():
                    console.print(f"  [dim]url[/dim]  {url.strip()}")
        else:
            size = f.stat().st_size
            console.print(f"  [dim]file[/dim] {f.name} ({size:,} bytes)")


def build_prompt_session(console: Console) -> PromptSession[str]:
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
        completer=WordCompleter(
            ["/quit", "/exit", "/q", "/help", "/status", "/doc", "/style add", "/style list"],
            sentence=True,
        ),
        key_bindings=kb,
        multiline=True,
        prompt_continuation=FormattedText([("class:prompt-continuation", "  ")]),
    )


def show_welcome(console: Console) -> None:
    lines = [
        "[bold]Inkwell[/bold] — AI writing agent",
        "",
        "Paste a Claude share link, URL, or writing brief.",
        "Type at any time — your input is picked up live.",
        "",
        "[dim]/status  pipeline progress     /style add <path>  add voice reference[/dim]",
        "[dim]/doc     Google Doc URL         /quit              exit[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(lines), border_style="blue", width=72))
    console.print()


async def chat_session(
    *,
    initial_task: str | None = None,
    session_id: str | None = None,
    verbose: bool = False,
) -> None:
    """Run an interactive chat session with the writing agent.

    The agent runs continuously through the writing pipeline and enters
    standby mode after completion, listening for further feedback. The
    user can type at any time — input is integrated live.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if session_id is None:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    console = Console(highlight=False)
    show_welcome(console)

    async def display_action(content: str) -> None:
        console.print(f"\n[blue][inkwell][/blue] {content}")

    reset_metrics()

    setup = setup_session(
        session_id,
        persistent=True,
        on_action=display_action,
    )

    pt_session = build_prompt_session(console)
    stack = AsyncExitStack()
    stack.enter_context(setup.sandbox)

    scheduler = setup.scheduler
    session_state = setup.session_state

    if scheduler is None:
        raise RuntimeError("Persistent mode required for chat session")

    try:
        async with stack:
            async with build_client(options=setup.options) as client:
                task = initial_task

                while True:
                    if task is None:
                        try:
                            user_input = await pt_session.prompt_async()
                        except (EOFError, asyncio.CancelledError):
                            break
                        except KeyboardInterrupt:
                            break

                        task = user_input.strip()
                        if not task:
                            continue
                        if task in ("/quit", "/exit", "/q"):
                            break
                        if handle_slash_command(task, session_state, console):
                            task = None
                            continue

                    console.print("[dim]starting...[/dim]")
                    await client.query(task)
                    collector = ResponseCollector(
                        client, trace_logger=setup.trace_logger
                    )
                    await collect_with_stdin(
                        collector, session_state, scheduler, console
                    )
                    task = None
    except KeyboardInterrupt:
        pass

    setup.trace_logger.save()
    log_metrics_summary()
    console.print("\n[dim]Session ended.[/dim]")
