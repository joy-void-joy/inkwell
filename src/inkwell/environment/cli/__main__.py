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
        typer.Option("--format", "-f", help="Suggested format (agent may override): lesswrong, twitter, blog, dialog, memo, or custom:<description>"),
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
    target_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Suggested format (agent may override): lesswrong, twitter, blog, dialog, memo, or custom:<description>"),
    ] = "lesswrong",
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Run the pipeline with a freeform task as source material.

    The task text is used as source for the planner — it extracts a
    structure and runs the full pipeline. For source links, use
    `inkwell write` instead.
    """
    from inkwell.environment.cli.chat import chat_session

    try:
        asyncio.run(
            chat_session(
                initial_task=task,
                session_id=session_id,
                target_format=target_format,
                verbose=verbose,
            )
        )
    except KeyboardInterrupt:
        pass


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

    from lup.paths import sessions_dir

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

        notes_dir = sessions_dir() / sid / "pipeline_notes"
        if notes_dir.exists():
            checkpoints = sorted(
                f.stem.removeprefix("snapshot_")
                for f in notes_dir.glob("snapshot_*.json")
            )
            if checkpoints:
                typer.echo(f"    checkpoints: {', '.join(checkpoints)}")


VALID_STAGES = [
    "extract", "voice", "plan", "research", "assumptions",
    "refine", "write", "merge", "review", "rewrite", "format",
]


@app.command()
def resume(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID to resume (see `inkwell sessions`)"),
    ],
    from_stage: Annotated[
        str | None,
        typer.Option(
            "--from", "-f",
            help="Resume from after this stage (e.g. 'write' to re-run merge onward). "
            "Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Resume a previous writing session from its saved pipeline state.

    By default, picks up from the last completed stage. Use --from to
    resume from an earlier checkpoint.

    Examples:
        inkwell sessions                              # find the session ID
        inkwell resume 20260523_143022                # resume from last stage
        inkwell resume 20260523_143022 --from write   # re-run merge onward
    """
    if from_stage and from_stage not in VALID_STAGES:
        typer.echo(
            f"Invalid stage '{from_stage}'. "
            f"Valid stages: {', '.join(VALID_STAGES)}"
        )
        raise typer.Exit(1)

    from inkwell.environment.cli.chat import chat_session

    try:
        asyncio.run(
            chat_session(
                resume_session_id=session_id,
                resume_from_stage=from_stage,
                verbose=verbose,
            )
        )
    except KeyboardInterrupt:
        pass


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
