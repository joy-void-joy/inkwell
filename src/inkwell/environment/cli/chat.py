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
import uuid
from datetime import timedelta

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.panel import Panel

from lup.telemetry.metrics import log_metrics_summary, reset_metrics
from lup.workspace.paths import project_root
from inkwell.agent.client import active_agents

from inkwell.agent.core import SessionTrace, run_session
from inkwell.agent.models import WritingOutput
from inkwell.agent.pipeline import DISPLAY_STAGES, PipelineError, PipelineListener
from inkwell.agent.session import WritingSessionState
from inkwell.agent.tools.google_docs import do_fetch_comments, do_insert_comment

logger = logging.getLogger(__name__)


STAGE_LABELS: dict[str, str] = {
    "extract": "📄 Extracting source material",
    "voice": "🎙️ Analyzing author's voice",
    "plan": "🗺️ Planning article structure",
    "assumptions": "❓ Surfacing questions for author",
    "research": "🔍 Researching claims",
    "refine": "📝 Refining the plan",
    "write": "✍️ Writing sections",
    "merge": "🧩 Merging into draft",
    "review": "🔎 Reviewing draft",
    "resolve": "🔧 Resolving open questions",
    "rewrite": "✨ Final rewrite",
    "format": "📐 Formatting output",
    "restart": "🔁 Replanning from author feedback",
    "revise": "🔄 Revising",
    "sync": "🔄 Syncing feedback",
}


SPINNER_FRAMES = "◇◈◆◈"


def running_for(elapsed: timedelta) -> str:
    """How long a turn has been working, at toolbar precision.

    Seconds under a minute, then whole minutes: the toolbar redraws several
    times a second, and a figure whose last digit never settles reads as
    noise rather than as progress.
    """
    seconds = int(elapsed.total_seconds())
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"


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
        if label and stage in DISPLAY_STAGES:
            label = f"[{DISPLAY_STAGES.index(stage) + 1}/{len(DISPLAY_STAGES)}] {label}"
        if label or active_agents:
            parts.append(
                (
                    "class:toolbar.spinner",
                    f" {SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]} ",
                )
            )
            frame += 1
        if label:
            parts.append(("class:toolbar.stage", f"{label} "))
        if active_agents:
            agents_str = "  ".join(
                f"{agent.label} {running_for(agent.elapsed())}"
                for agent in sorted(active_agents, key=lambda a: a.started)
            )
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
        *,
        session_id: str = "",
    ) -> None:
        super().__init__()
        self.console = console
        self.input_queue = input_queue
        self.session_id = session_id
        self.current_stage = ""

    async def on_stage(self, stage: str, description: str) -> None:
        self.current_stage = stage
        label = STAGE_LABELS.get(stage, stage)
        counter = ""
        if stage in DISPLAY_STAGES:
            counter = f" [dim]\\[{DISPLAY_STAGES.index(stage) + 1}/{len(DISPLAY_STAGES)}][/dim]"
        self.console.print(
            f"\n  [bold blue]{label}[/bold blue]{counter}  [dim]{description}[/dim]"
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
                expand=False,
            )
        )

    async def on_pause(self, stage: str, doc_url: str) -> None:
        label = STAGE_LABELS.get(stage, stage)
        resume_cmd = (
            f"inkwell resume {self.session_id}"
            if self.session_id
            else ("inkwell resume <session-id>")
        )
        self.console.print()
        self.console.print(
            Panel(
                f"Stopped after [bold]{label}[/bold] as requested.\n\n"
                f"[dim]Doc:[/dim]    {doc_url}\n\n"
                "Review and comment in the Doc, then continue with:\n"
                f"  [bold]{resume_cmd}[/bold]",
                title="⏸ Paused",
                border_style="yellow",
                expand=False,
            )
        )

    async def on_message(self, source: str, message: str) -> None:
        self.console.print(f"  💬 [dim]{message}[/dim]")

    async def collect_author_input(self, state: WritingSessionState) -> list[str]:
        items: list[str] = []
        while not self.input_queue.empty():
            try:
                items.append(self.input_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        for msg in items:
            self.console.print(f"  [cyan]Author direction: {msg}[/cyan]")
            if state.doc_id:
                try:
                    await do_insert_comment(
                        state.doc_id,
                        f"[TERMINAL] {msg}",
                        session_state=state,
                    )
                except (RuntimeError, OSError):
                    logger.warning("Failed to mirror terminal input to GDoc")

        return items

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
        "[dim]/status  pipeline stage    /doc    open Google Doc[/dim]",
        "[dim]/sync   poll edits+comments  /style  manage corpus[/dim]",
        "[dim]/fetch <doc>  pull comments   /done   finish revisions[/dim]",
        "[dim]/quit  exit  /help  commands[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(lines), border_style="blue", width=72))
    console.print()


def show_help(console: Console) -> None:
    console.print(
        "\n  [bold]Commands:[/bold]\n"
        "  /sync          Poll GDoc comments + tab edits, trigger revision\n"
        "  /fetch <doc>   Fetch comments from a Google Doc and merge as feedback\n"
        "  /status        Show current pipeline stage\n"
        "  /doc           Show Google Doc URL\n"
        "  /style         Show style corpus entries\n"
        "  /done          Finish revision loop\n"
        "  /quit          Exit session\n"
        "  /help          Show this help\n"
    )


async def fetch_and_queue_comments(
    doc_arg: str,
    input_queue: asyncio.Queue[str],
    console: Console,
    session_state: WritingSessionState | None = None,
) -> None:
    """Fetch comments from a Google Doc and queue them as feedback."""
    from inkwell.agent.tools.extract import is_gdoc_url, parse_gdoc_id

    try:
        if is_gdoc_url(doc_arg):
            doc_id = parse_gdoc_id(doc_arg)
        else:
            doc_id = doc_arg

        already_seen = set[str]()
        if session_state:
            already_seen = (
                session_state.seen_comment_ids | session_state.agent_comment_ids
            )

        comments = await do_fetch_comments(
            doc_id, exclude_ids=already_seen, include_resolved=True
        )
    except (RuntimeError, OSError, ValueError) as exc:
        console.print(f"  [red]Failed to fetch comments: {exc}[/red]")
        return

    if not comments:
        console.print("  [dim]No new comments found on that doc.[/dim]")
        return

    for c in comments:
        tag = "Resolved GDoc Comment" if c.resolved else "GDoc Comment"
        line = f"[{tag} from {doc_id}] {c.content}"
        if c.anchor_text:
            line += f' (on: "{c.anchor_text}")'
        if c.replies:
            line += f" | Replies: {' → '.join(c.replies)}"
        input_queue.put_nowait(line)

    console.print(
        f"  [yellow]Fetched {len(comments)} comment(s) from doc — "
        f"queued as feedback[/yellow]"
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

            voice_entries, prescriptive_entries = list_style_references()
            if not voice_entries and not prescriptive_entries:
                console.print("  [dim]No style corpus. Use `inkwell style add`.[/dim]")
            else:
                for e in voice_entries:
                    console.print(f"  [dim][{e.kind}] {e.name}[/dim]")
                for e in prescriptive_entries:
                    console.print(f"  [dim][prescriptive] {e.name}[/dim]")
            continue
        if stripped == "/sync":
            if listener:
                listener.request_sync()
                console.print(
                    "  [yellow]Sync requested — polling comments + edits[/yellow]"
                )
            else:
                console.print("  [dim]No active pipeline to sync.[/dim]")
            continue
        if stripped.startswith("/fetch "):
            doc_arg = stripped.removeprefix("/fetch ").strip()
            if not doc_arg:
                console.print("  [red]Usage: /fetch <google_doc_url_or_id>[/red]")
                continue
            asyncio.create_task(
                fetch_and_queue_comments(doc_arg, input_queue, console, session_state)
            )
            continue
        if stripped.startswith("/") and stripped not in ("/done",):
            console.print(f"  [red]Unknown command: {stripped}[/red]")
            continue

        input_queue.put_nowait(stripped)
        console.print(f"  [dim]Queued: {stripped[:60]}[/dim]")


async def chat_session(
    *,
    sources: list[str] | None = None,
    refs: list[str] | None = None,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    resume_from_stage: str | None = None,
    target_format: str = "auto",
    existing_doc_id: str | None = None,
    stop_after: str | None = None,
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
        session_id = resume_session_id or uuid.uuid4().hex[:16]

    console = Console(highlight=False)
    show_welcome(console)

    reset_metrics()

    input_queue: asyncio.Queue[str] = asyncio.Queue()
    listener = InteractiveListener(console, input_queue, session_id=session_id)
    pt_session = build_prompt_session(listener)

    with patch_stdout(raw=True):
        resolved_sources = list(sources or [])
        if not resolved_sources and resume_session_id is None:
            try:
                raw = await pt_session.prompt_async()
            except (EOFError, KeyboardInterrupt):
                return

            raw = raw.strip()
            if not raw or raw in ("/quit", "/exit", "/q"):
                return
            resolved_sources = [raw]

        if resolved_sources:
            label = ", ".join(s[:40] for s in resolved_sources)
            if len(label) > 80:
                label = label[:77] + "..."
            console.print(f"  [dim]Sources: {label}[/dim]")
        elif resume_session_id:
            console.print(f"  [dim]Resuming session: {resume_session_id}[/dim]")

        trace = SessionTrace()
        session_state = WritingSessionState()

        input_task = asyncio.create_task(
            read_terminal_input(
                pt_session, input_queue, console, listener, session_state
            )
        )

        from inkwell.agent.config import active_profile, load_settings, use_settings

        resolved_profile = active_profile()
        if not resolved_profile and resume_session_id:
            from inkwell.agent.core import load_snapshot

            snapshot = load_snapshot(resume_session_id, from_stage=resume_from_stage)
            if snapshot:
                resolved_profile = snapshot.profile
        session_settings = load_settings(resolved_profile)

        try:
            with use_settings(session_settings):
                result = await run_session(
                    sources=resolved_sources,
                    refs=refs,
                    resume_session_id=resume_session_id,
                    resume_from_stage=resume_from_stage,
                    target_format=target_format,
                    existing_doc_id=existing_doc_id,
                    session_id=session_id,
                    listener=listener,
                    trace=trace,
                    session_state=session_state,
                    stop_after=stop_after,
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
                    for name, vals in sorted(grouped.items(), key=lambda kv: -kv[1][0]):
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
            trace_path = trace.save()
            if trace_path:
                console.print(f"\n  [dim]Trace saved to: {trace_path}[/dim]")

    log_metrics_summary()
    console.print("\n[dim]Session ended.[/dim]")
