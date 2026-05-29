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


@app.command("fetch-comments")
def fetch_comments(
    doc: Annotated[
        str,
        typer.Argument(help="Google Doc URL or document ID"),
    ],
    session_id: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Session ID to merge comments into (queues as feedback)"),
    ] = None,
) -> None:
    """Fetch and display comments from a Google Doc.

    Without --session, prints all unresolved comments. With --session,
    merges them into the session's feedback pipeline for the next stage.

    Examples:
        inkwell fetch-comments "https://docs.google.com/document/d/abc123/edit"
        inkwell fetch-comments abc123 --session 20260523_143022
    """
    from inkwell.agent.tools.extract import GDOC_URL_PATTERN, parse_gdoc_id
    from inkwell.agent.tools.google_docs import do_fetch_comments

    if GDOC_URL_PATTERN.search(doc):
        doc_id = parse_gdoc_id(doc)
    else:
        doc_id = doc

    comments = asyncio.run(do_fetch_comments(doc_id))

    if not comments:
        typer.echo("No unresolved comments found.")
        return

    typer.echo(f"{len(comments)} comment(s):\n")
    for c in comments:
        anchor = f' (on: "{c.anchor_text}")' if c.anchor_text else ""
        typer.echo(f"  [{c.author}] {c.content}{anchor}")
        for reply in c.replies:
            typer.echo(f"    → {reply}")
        typer.echo()

    if session_id:
        from lup.paths import sessions_dir

        notes_dir = sessions_dir() / session_id / "pipeline_notes"
        if not notes_dir.exists():
            typer.echo(f"Session '{session_id}' not found — comments printed but not merged.")
            return

        feedback_file = notes_dir / "fetched_comments.md"
        lines: list[str] = [f"# Fetched Comments from {doc_id}\n"]
        for c in comments:
            anchor = f' (on: "{c.anchor_text}")' if c.anchor_text else ""
            lines.append(f"- [{c.author}] {c.content}{anchor}")
            for reply in c.replies:
                lines.append(f"  - Reply: {reply}")
        feedback_file.write_text("\n".join(lines), encoding="utf-8")
        typer.echo(f"Merged {len(comments)} comment(s) into session {session_id}")


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
    target_format: Annotated[
        str | None,
        typer.Option("--format", "-f", help="Add as format-specific example (lesswrong, twitter, blog, dialog, memo)"),
    ] = None,
) -> None:
    """Add a writing sample to the style corpus.

    Without --format, the sample is used as a general voice reference.
    With --format, it's used as an example of good output in that format.

    Examples:
        inkwell style add ~/writing/my-essay.md
        inkwell style add "https://lesswrong.com/posts/my-post" --format lesswrong
        inkwell style add ~/memos/good-memo.md -f memo
    """
    from inkwell.agent.tools.voice import add_style_reference

    typer.echo(add_style_reference(source, target_format=target_format))


@style_app.command("list")
def style_list() -> None:
    """List the current style corpus and format-specific examples."""
    from inkwell.agent.tools.voice import list_format_examples, list_style_references

    entries = list_style_references()
    format_entries = list_format_examples()

    if not entries and not format_entries:
        typer.echo("No style corpus configured. Use `inkwell style add` to start.")
        return

    if entries:
        typer.echo("Voice references:")
        for entry in entries:
            if entry.kind == "url":
                typer.echo(f"  [url]  {entry.name}")
            else:
                typer.echo(f"  [file] {entry.name} ({entry.size:,} bytes)")

    if format_entries:
        typer.echo("")
        typer.echo("Format examples:")
        for fmt, fmt_list in sorted(format_entries.items()):
            typer.echo(f"  {fmt}:")
            for entry in fmt_list:
                if entry.kind == "url":
                    typer.echo(f"    [url]  {entry.name}")
                else:
                    typer.echo(f"    [file] {entry.name} ({entry.size:,} bytes)")


if __name__ == "__main__":
    app()
