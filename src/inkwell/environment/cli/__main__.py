"""Inkwell CLI — interactive writing agent.

Usage:
    inkwell                                          # open interactive chat
    inkwell write "https://claude.ai/share/abc123"   # run the pipeline
    inkwell style add "https://lesswrong.com/..."     # manage voice corpus
"""

import asyncio
import logging
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import urlparse

import typer

logger = logging.getLogger(__name__)


def extract_doc_id_from_url(url: str) -> str:
    """Extract a Google Doc ID from a URL like docs.google.com/document/d/{id}/edit."""
    path = PurePosixPath(urlparse(url).path)
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "d" and i + 1 < len(parts):
            return parts[i + 1]
    return ""

app = typer.Typer(
    name="inkwell",
    help="AI writing agent — transforms conversations into polished articles",
    no_args_is_help=False,
    add_completion=False,
)

style_app = typer.Typer(
    name="style",
    help="Manage the style reference corpus",
    no_args_is_help=True,
)
app.add_typer(style_app, name="style")


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context) -> None:
    """AI writing agent — opens interactive chat when run with no arguments."""
    if ctx.invoked_subcommand is None:
        from inkwell.environment.cli.chat import chat_session

        try:
            asyncio.run(chat_session())
        except KeyboardInterrupt:
            pass


@app.command()
def write(
    source: Annotated[
        str,
        typer.Argument(help="Claude share link, URL, or writing brief"),
    ],
    ref: Annotated[
        list[str] | None,
        typer.Option("--ref", "-r", help="Additional reference URLs or file paths"),
    ] = None,
    target_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Target format: lesswrong, twitter, blog"),
    ] = "lesswrong",
    doc: Annotated[
        str | None,
        typer.Option("--doc", "-d", help="Existing Google Doc URL or ID to write into"),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Write an article from source material.

    SOURCE can be:
    - A Claude.ai share link (https://claude.ai/share/...)
    - A URL to extract content from
    - A local file path

    Runs the full pipeline (plan, research, write, review, rewrite)
    with live progress in the terminal and a Google Doc for collaboration.

    Examples:
        inkwell write "https://claude.ai/share/abc123"
        inkwell write "https://claude.ai/share/abc123" --ref "paper.pdf" -f twitter
        inkwell write "https://claude.ai/share/abc123" --doc "https://docs.google.com/document/d/..."
    """
    from inkwell.environment.cli.chat import chat_session

    doc_id: str | None = None
    if doc:
        if doc.startswith("http"):
            doc_id = extract_doc_id_from_url(doc)
        else:
            doc_id = doc

    try:
        asyncio.run(
            chat_session(
                initial_task=source,
                session_id=session_id,
                target_format=target_format,
                refs=ref,
                existing_doc_id=doc_id,
                verbose=verbose,
            )
        )
    except KeyboardInterrupt:
        pass


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="Freeform task for the agent")],
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Run the agent with a freeform task (no pipeline).

    For structured article writing, use `inkwell write` instead.
    This mode gives the agent all tools and lets it work freely.
    """
    from rich.console import Console

    from inkwell.agent.core import run_agent

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    console = Console(highlight=False)
    console.print("[dim]Running agent...[/dim]")

    async def on_action(content: str) -> None:
        console.print(f"  [dim]{content[:120]}[/dim]")

    try:
        result = asyncio.run(
            run_agent(
                task,
                session_id=session_id,
                persistent=False,
                on_action=on_action,
            )
        )
        console.print(f"\n[bold]Title:[/bold] {result.output.title}")
        if result.output.google_doc_url:
            console.print(f"[bold]Doc:[/bold]   {result.output.google_doc_url}")
        console.print(f"[bold]Words:[/bold] {result.output.word_count}")
        if result.cost_usd is not None:
            console.print(f"[bold]Cost:[/bold]  ${result.cost_usd:.2f}")
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except (RuntimeError, OSError, ConnectionError) as exc:
        console.print(f"\n[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def sessions(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Max sessions to show"),
    ] = 20,
) -> None:
    """List past writing sessions."""
    from lup.history import get_latest_session_json, list_all_sessions

    session_ids = list_all_sessions()
    if not session_ids:
        typer.echo("No sessions found.")
        return

    for sid in session_ids[-limit:]:
        data = get_latest_session_json(sid)
        if data is None:
            typer.echo(f"  {sid}  (no data)")
            continue

        title = ""
        doc_url = ""
        output = data.get("output")
        if isinstance(output, dict):
            t = output.get("title")
            if isinstance(t, str):
                title = t
            u = output.get("google_doc_url")
            if isinstance(u, str):
                doc_url = u

        cost = data.get("cost_usd")

        parts = [f"  {sid}"]
        if title:
            parts.append(f"  {title}")
        if isinstance(cost, (int, float)):
            parts.append(f"  ${cost:.2f}")
        typer.echo("".join(parts))
        if doc_url:
            typer.echo(f"    {doc_url}")


@app.command()
def resume(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID to resume (see `inkwell sessions`)"),
    ],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Resume a previous writing session.

    Loads the session's Google Doc and pipeline state, then continues
    the agent with context about what was already done.

    Examples:
        inkwell sessions                      # find the session ID
        inkwell resume 20260523_143022        # resume it
    """
    from lup.history import get_latest_session_json

    data = get_latest_session_json(session_id)
    if data is None:
        typer.echo(f"Session '{session_id}' not found. Use `inkwell sessions` to list.")
        raise typer.Exit(1)

    output = data.get("output")
    doc_id = ""
    doc_url = ""
    title = ""
    summary = ""
    review_findings: list[object] = []
    open_questions: list[str] = []
    voice_summary = ""
    if isinstance(output, dict):
        d = output.get("google_doc_id")
        if isinstance(d, str):
            doc_id = d
        u = output.get("google_doc_url")
        if isinstance(u, str):
            doc_url = u
            if not doc_id:
                doc_id = extract_doc_id_from_url(u)
        t = output.get("title")
        if isinstance(t, str):
            title = t
        s = output.get("summary")
        if isinstance(s, str):
            summary = s
        rf = output.get("review_findings")
        if isinstance(rf, list):
            review_findings = rf
        oq = output.get("open_questions")
        if isinstance(oq, list):
            open_questions = [str(q) for q in oq]
        vp = output.get("voice_profile")
        if isinstance(vp, dict):
            vs = vp.get("summary")
            if isinstance(vs, str):
                voice_summary = vs

    task_parts = [f"Resume the writing session for: {title or session_id}"]
    if doc_url:
        task_parts.append(f"\nGoogle Doc: {doc_url}")
    if summary:
        task_parts.append(f"\nPrevious session summary: {summary}")
    if voice_summary:
        task_parts.append(f"\nVoice profile: {voice_summary}")
    if open_questions:
        task_parts.append(
            "\nOpen questions from previous session:\n"
            + "\n".join(f"- {q}" for q in open_questions)
        )
    if review_findings:
        critical = [
            f
            for f in review_findings
            if isinstance(f, dict) and f.get("severity") == "critical"
        ]
        if critical:
            task_parts.append(f"\n{len(critical)} unresolved critical review findings.")
    task_parts.append(
        "\nRead the Google Doc to understand current state. "
        "Check for author comments and continue where the session left off."
    )

    from rich.console import Console

    from inkwell.agent.core import run_agent

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    console = Console(highlight=False)

    async def on_action(content: str) -> None:
        console.print(f"  [dim]{content[:120]}[/dim]")

    try:
        result = asyncio.run(
            run_agent(
                "\n".join(task_parts),
                existing_doc_id=doc_id or None,
                session_id=session_id,
                persistent=True,
                on_action=on_action,
            )
        )
        console.print(f"\n[bold]Title:[/bold] {result.output.title}")
        if result.output.google_doc_url:
            console.print(f"[bold]Doc:[/bold]   {result.output.google_doc_url}")
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except (RuntimeError, OSError, ConnectionError) as exc:
        console.print(f"\n[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def setup() -> None:
    """Run the setup wizard to configure integrations."""
    from inkwell.devtools.setup import main as setup_main

    ctx = typer.Context(typer.main.get_command(app))
    setup_main(ctx)


@style_app.command("add")
def style_add(
    source: Annotated[
        str,
        typer.Argument(help="URL or file path of a style reference"),
    ],
) -> None:
    """Add a writing sample to the style corpus.

    The agent uses the style corpus to match your voice.

    Examples:
        inkwell style add "https://lesswrong.com/posts/my-post"
        inkwell style add ~/writing/my-essay.md
    """
    from inkwell.agent.tools.voice import add_style_reference

    typer.echo(add_style_reference(source))


@style_app.command("list")
def style_list() -> None:
    """List the current style corpus."""
    from inkwell.agent.tools.voice import list_style_references

    entries = list_style_references()
    if not entries:
        typer.echo("No style corpus configured. Use `inkwell style add` to start.")
        return

    typer.echo("Style corpus:")
    for entry in entries:
        if entry.kind == "url":
            typer.echo(f"  [url]  {entry.name}")
        else:
            typer.echo(f"  [file] {entry.name} ({entry.size:,} bytes)")


if __name__ == "__main__":
    app()
