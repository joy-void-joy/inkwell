"""Inkwell CLI — interactive writing agent.

Usage:
    inkwell                                          # open interactive chat
    inkwell write "https://claude.ai/share/abc123"   # run the pipeline
    inkwell style add "https://lesswrong.com/..."     # manage voice corpus
"""

import asyncio
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlparse

import typer

from inkwell.agent.stages import OUTPUT_FORMATS

FORMAT_HELP = "Suggested format (agent may override): " + ", ".join(
    f"{f.key}:<description>" if f.accepts_description else f.key for f in OUTPUT_FORMATS
)

STOP_AFTER_HELP = (
    "Pause after this stage finishes, then exit cleanly — review and comment in "
    "the Doc, then `inkwell resume <id>` to continue. Stages: extract, voice, "
    "plan, research, assumptions, refine, write, merge, review, rewrite, format"
)


def validate_stage_option(value: str | None, flag: str) -> None:
    """Reject a stage flag that doesn't name a resumable checkpoint stage."""
    if value is None:
        return
    from inkwell.agent.pipeline import CHECKPOINT_STAGES

    if value not in CHECKPOINT_STAGES:
        typer.echo(
            f"Invalid {flag} '{value}'. Valid stages: {', '.join(CHECKPOINT_STAGES)}"
        )
        raise typer.Exit(1)


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
def callback(
    ctx: typer.Context,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Configuration profile to use (e.g. 'work', 'personal')",
            envvar="INKWELL_PROFILE",
        ),
    ] = None,
) -> None:
    """AI writing agent — opens interactive chat when run with no arguments."""
    if profile:
        os.environ["INKWELL_PROFILE"] = profile
    if ctx.invoked_subcommand is None:
        from inkwell.environment.cli.chat import chat_session

        try:
            asyncio.run(chat_session())
        except KeyboardInterrupt:
            pass


@app.command()
def write(
    sources: Annotated[
        list[str] | None,
        typer.Argument(
            help="Source materials: Claude share links, URLs, file paths, or a writing brief"
        ),
    ] = None,
    ref: Annotated[
        list[str] | None,
        typer.Option("--ref", "-r", help="Supplementary reference URLs or file paths"),
    ] = None,
    target_format: Annotated[
        str,
        typer.Option("--format", "-f", help=FORMAT_HELP),
    ] = "auto",
    doc: Annotated[
        str | None,
        typer.Option("--doc", "-d", help="Existing Google Doc URL or ID to write into"),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option("--stop-after", help=STOP_AFTER_HELP),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Write an article from source material.

    Each SOURCE can be:
    - A Claude.ai share link (https://claude.ai/share/...)
    - A URL to extract content from
    - A local file path

    Multiple sources are extracted and combined for the pipeline.

    Examples:
        inkwell write "https://claude.ai/share/abc123"
        inkwell write "https://claude.ai/share/abc123" paper.pdf -f twitter
        inkwell write conversation.md --stop-after plan   # pause to review the plan
    """
    from inkwell.environment.cli.chat import chat_session

    validate_stage_option(stop_after, "--stop-after")

    doc_id: str | None = None
    if doc:
        if doc.startswith("http"):
            doc_id = extract_doc_id_from_url(doc)
        else:
            doc_id = doc

    try:
        asyncio.run(
            chat_session(
                sources=sources or [],
                refs=ref,
                session_id=session_id,
                target_format=target_format,
                existing_doc_id=doc_id,
                stop_after=stop_after,
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
        typer.Option("--format", "-f", help=FORMAT_HELP),
    ] = "auto",
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option("--stop-after", help=STOP_AFTER_HELP),
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

    validate_stage_option(stop_after, "--stop-after")

    try:
        asyncio.run(
            chat_session(
                sources=[task],
                session_id=session_id,
                target_format=target_format,
                stop_after=stop_after,
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
    from lup.workspace.history import latest_session_record, list_all_session_ids

    session_ids = list_all_session_ids()
    if not session_ids:
        typer.echo("No sessions found.")
        return

    from lup.workspace.paths import sessions_dir

    for sid in session_ids[-limit:]:
        record = latest_session_record(sid)
        if record is None:
            typer.echo(f"  {sid}  (no data)")
            continue

        # lup: ignore[dict-get] — a record keeps `output` raw, because the
        # output model belongs to the domain and is not known at read time
        title = record.output.get("title")
        doc_url = record.output.get("google_doc_url")  # lup: ignore[dict-get]

        parts = [f"  {sid}"]
        if isinstance(title, str):
            parts.append(f"  {title}")
        if record.cost_usd is not None:
            parts.append(f"  ${record.cost_usd:.2f}")
        typer.echo("".join(parts))
        if isinstance(doc_url, str):
            typer.echo(f"    {doc_url}")

        notes_dir = sessions_dir() / sid / "pipeline_notes"
        if notes_dir.exists():
            checkpoints = sorted(
                f.stem.removeprefix("snapshot_")
                for f in notes_dir.glob("snapshot_*.json")
            )
            if checkpoints:
                typer.echo(f"    checkpoints: {', '.join(checkpoints)}")


@app.command()
def resume(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID to resume (see `inkwell sessions`)"),
    ],
    from_stage: Annotated[
        str | None,
        typer.Option(
            "--from",
            "-f",
            help="Resume from after this stage (e.g. 'write' to re-run merge onward). "
            "Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option("--stop-after", help=STOP_AFTER_HELP),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Resume a previous writing session from its saved pipeline state.

    By default, picks up from the last completed stage. Use --from to
    resume from an earlier checkpoint, and --stop-after to pause again at a
    later one.

    Examples:
        inkwell sessions                              # find the session ID
        inkwell resume 20260523_143022                # resume from last stage
        inkwell resume 20260523_143022 --from write   # re-run merge onward
        inkwell resume 20260523_143022 --stop-after review  # pause after review
    """
    validate_stage_option(from_stage, "--from")
    validate_stage_option(stop_after, "--stop-after")

    from inkwell.environment.cli.chat import chat_session

    try:
        asyncio.run(
            chat_session(
                resume_session_id=session_id,
                resume_from_stage=from_stage,
                stop_after=stop_after,
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
        typer.Option(
            "--session",
            "-s",
            help="Session ID to merge comments into (queues as feedback)",
        ),
    ] = None,
) -> None:
    """Fetch and display comments from a Google Doc.

    Without --session, prints all unresolved comments. With --session,
    merges them into the session's feedback pipeline for the next stage.

    Examples:
        inkwell fetch-comments "https://docs.google.com/document/d/abc123/edit"
        inkwell fetch-comments abc123 --session 20260523_143022
    """
    from inkwell.agent.tools.extract import is_gdoc_url, parse_gdoc_id
    from inkwell.agent.tools.google_docs import do_fetch_comments

    if is_gdoc_url(doc):
        doc_id = parse_gdoc_id(doc)
    else:
        doc_id = doc

    comments = asyncio.run(do_fetch_comments(doc_id, include_resolved=True))

    if not comments:
        typer.echo("No comments found.")
        return

    typer.echo(f"{len(comments)} comment(s):\n")
    for c in comments:
        anchor = f' (on: "{c.anchor_text}")' if c.anchor_text else ""
        typer.echo(f"  [{c.author}] {c.content}{anchor}")
        for reply in c.replies:
            typer.echo(f"    → {reply}")
        typer.echo()

    if session_id:
        from lup.workspace.paths import sessions_dir

        notes_dir = sessions_dir() / session_id / "pipeline_notes"
        if not notes_dir.exists():
            typer.echo(
                f"Session '{session_id}' not found — comments printed but not merged."
            )
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
def extract(
    source: Annotated[
        str,
        typer.Argument(help="Claude share link, URL, or file path to extract"),
    ],
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output file path (default: stdout)"),
    ] = None,
) -> None:
    """Extract source material to a local file for offline use.

    Extracts content from a Claude share link, URL, or file and saves it
    as markdown. Useful for pre-extracting conversations while your session
    cookie is fresh, then running `inkwell write` later in headless environments.

    Examples:
        inkwell extract "https://claude.ai/share/abc123" -o conversation.md
        inkwell extract "https://lesswrong.com/posts/.../slug" -o post.md
        inkwell write conversation.md   # no cookie needed
    """
    import tempfile
    from pathlib import Path as P

    from inkwell.agent.tools.research.fetch import do_fetch_source
    from lup.workspace.content_safety import configure
    from lup.mcp import ToolError

    configure(directory=P(tempfile.mkdtemp(prefix="inkwell-extract-")))

    try:
        result = asyncio.run(do_fetch_source(source))
        text = P(result.content.path).read_text(encoding="utf-8")
    except (RuntimeError, ToolError) as e:
        typer.echo(f"Extraction failed: {e}", err=True)
        raise typer.Exit(1)

    if output:
        from pathlib import Path

        out_path = Path(output)
        out_path.write_text(text, encoding="utf-8")
        typer.echo(f"Extracted to {out_path}")
    else:
        typer.echo(text)


def register_setup() -> None:
    from inkwell.devtools.setup import app as setup_app

    app.add_typer(setup_app, name="setup", help="Configure integrations")


register_setup()


@style_app.command("add")
def style_add(
    source: Annotated[
        str,
        typer.Argument(help="URL or file path of a style reference"),
    ],
    target_format: Annotated[
        str | None,
        typer.Option(
            "--format",
            "-f",
            help="Add as format-specific example ("
            + ", ".join(f.key for f in OUTPUT_FORMATS if not f.accepts_description)
            + ")",
        ),
    ] = None,
    prescriptive: Annotated[
        bool,
        typer.Option(
            "--prescriptive",
            "-p",
            help="Treat as prescriptive rules (passed verbatim, not analyzed for voice)",
        ),
    ] = False,
) -> None:
    """Add a writing sample to the style corpus.

    Without flags, the sample is analyzed for voice characteristics.
    With --prescriptive, the document is passed through verbatim as
    hard editing rules (style guides, checklists, do/don't lists).
    With --format, it's used as an example of good output in that format.

    Examples:
        inkwell style add ~/writing/my-essay.md
        inkwell style add ~/editing/style-guide.md --prescriptive
        inkwell style add "https://lesswrong.com/posts/my-post" --format lesswrong
    """
    from inkwell.agent.tools.voice import add_style_reference

    typer.echo(
        add_style_reference(
            source, target_format=target_format, prescriptive=prescriptive
        )
    )


@style_app.command("list")
def style_list() -> None:
    """List the current style corpus and format-specific examples."""
    from inkwell.agent.tools.voice import list_format_examples, list_style_references

    voice_entries, prescriptive_entries = list_style_references()
    format_entries = list_format_examples()

    if not voice_entries and not prescriptive_entries and not format_entries:
        typer.echo("No style corpus configured. Use `inkwell style add` to start.")
        return

    if voice_entries:
        typer.echo("Voice references:")
        for entry in voice_entries:
            if entry.kind == "url":
                typer.echo(f"  [url]  {entry.name}")
            else:
                typer.echo(f"  [file] {entry.name} ({entry.size:,} bytes)")

    if prescriptive_entries:
        typer.echo("")
        typer.echo("Prescriptive rules (passed verbatim):")
        for entry in prescriptive_entries:
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


cache_app = typer.Typer(
    name="cache",
    help="Manage local caches",
    no_args_is_help=True,
)
app.add_typer(cache_app, name="cache")


@cache_app.command("clear")
def cache_clear(
    voice: Annotated[
        bool,
        typer.Option("--voice", help="Clear only voice analysis cache"),
    ] = False,
    urls: Annotated[
        bool,
        typer.Option("--urls", help="Clear only URL fetch cache"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation"),
    ] = False,
) -> None:
    """Clear cached voice analyses and URL fetches.

    By default clears everything. Use --voice or --urls to target
    a specific cache.

    Examples:
        inkwell cache clear           # clear all caches
        inkwell cache clear --voice   # clear voice analyses only
        inkwell cache clear --urls    # clear URL fetch cache only
    """
    import shutil

    from inkwell.agent.config import settings

    style_root = Path(settings.style_corpus_path)
    cache_root = style_root / ".cache"

    if not cache_root.exists():
        typer.echo("No caches found.")
        return

    clear_all = not voice and not urls
    targets: list[tuple[str, Path]] = []

    voice_dir = cache_root / "voice"
    url_cache_files = list(cache_root.glob("*.txt"))

    if (clear_all or voice) and voice_dir.exists():
        count = sum(1 for _ in voice_dir.rglob("*") if _.is_file())
        targets.append((f"voice analyses ({count} files)", voice_dir))

    if (clear_all or urls) and url_cache_files:
        targets.append((f"URL fetch cache ({len(url_cache_files)} files)", cache_root))

    if not targets:
        typer.echo("Nothing to clear.")
        return

    for label, _ in targets:
        typer.echo(f"  {label}")

    if not yes:
        typer.confirm("Delete these caches?", abort=True)

    for label, path in targets:
        if path == cache_root:
            for f in url_cache_files:
                f.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)

    typer.echo("Cleared.")


@cache_app.command("status")
def cache_status() -> None:
    """Show cache sizes and file counts."""
    from inkwell.agent.config import settings

    style_root = Path(settings.style_corpus_path)
    cache_root = style_root / ".cache"

    if not cache_root.exists():
        typer.echo("No caches found.")
        return

    voice_dir = cache_root / "voice"
    url_files = list(cache_root.glob("*.txt"))

    if voice_dir.exists():
        analyses = list(voice_dir.glob("*.md"))
        inputs = (
            list((voice_dir / "inputs").glob("*"))
            if (voice_dir / "inputs").exists()
            else []
        )
        merged = voice_dir / "merged_guide.md"
        typer.echo(
            f"Voice cache:  {len(analyses)} analyses, {len(inputs)} inputs"
            + (" + merged guide" if merged.exists() else "")
        )
    else:
        typer.echo("Voice cache:  empty")

    typer.echo(f"URL cache:    {len(url_files)} fetched URLs")


if __name__ == "__main__":
    app()
