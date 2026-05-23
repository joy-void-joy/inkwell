"""Inkwell CLI — interactive writing agent.

Usage:
    inkwell write "https://claude.ai/share/abc123"
    inkwell write "https://claude.ai/share/abc123" --ref "https://arxiv.org/..."
    inkwell style add "https://lesswrong.com/posts/my-best-post"
    inkwell style list
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import sh
import typer

from inkwell.agent.config import settings
from inkwell.agent.core import run_agent
from inkwell.agent.models import AgentSessionResult

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="inkwell",
    help="AI writing agent — transforms conversations into polished articles",
    no_args_is_help=True,
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
    """AI writing agent."""
    if ctx.invoked_subcommand is None:
        raise typer.Exit()


async def run_session(
    task: str,
    *,
    session_id: str | None = None,
    persistent: bool = True,
) -> AgentSessionResult:
    logger.info("Starting session with model: %s", settings.model)

    async def on_action(content: str) -> None:
        typer.echo(f"\n[inkwell] {content}")

    result = await run_agent(
        task,
        session_id=session_id,
        persistent=persistent,
        on_action=on_action if persistent else None,
    )

    logger.info(
        "Session %s completed (cost: $%.4f, duration: %.1fs)",
        result.session_id,
        result.cost_usd or 0,
        result.duration_seconds or 0,
    )

    return result


def commit_results() -> None:
    git = sh.Command("git")
    status = str(git.status("--porcelain", "--", "notes/", _ok_code=[0])).strip()
    if not status:
        return

    try:
        git.add("notes/")
        diff = str(git.diff("--cached", "--stat", _ok_code=[0, 1])).strip()
        if diff:
            git.commit("-m", "data(writing): auto-commit session results")
            typer.echo("Committed session results.")
    except sh.ErrorReturnCode as e:
        logger.warning("Auto-commit failed: %s", e)


def print_result(result: AgentSessionResult) -> None:
    typer.echo(f"\nSession: {result.session_id}")
    typer.echo(f"Title: {result.output.title}")
    if result.output.google_doc_url:
        typer.echo(f"Google Doc: {result.output.google_doc_url}")
    typer.echo(f"Sections: {result.output.sections_completed}")
    typer.echo(f"Word count: {result.output.word_count}")
    if result.output.open_questions:
        typer.echo(f"Open questions: {len(result.output.open_questions)}")
    if result.cost_usd:
        typer.echo(f"Cost: ${result.cost_usd:.4f}")
    if result.duration_seconds:
        typer.echo(f"Duration: {result.duration_seconds:.1f}s")


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
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    no_persist: Annotated[
        bool,
        typer.Option("--no-persist", help="Run as one-shot (no sleep/wake loop)"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Write an article from source material.

    SOURCE can be:
    - A Claude.ai share link (https://claude.ai/share/...)
    - A URL to extract content from
    - A direct writing brief as text

    The agent writes into a Google Doc you can follow in real time.
    It sleeps at checkpoints so you can leave comments, then continues.

    Examples:
        inkwell write "https://claude.ai/share/abc123"
        inkwell write "https://claude.ai/share/abc123" --ref "paper.pdf" -f twitter
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    task_parts = [f"Write a {target_format} article from this source: {source}"]

    if ref:
        task_parts.append("\nAdditional references:")
        for r in ref:
            task_parts.append(f"- {r}")

    style_dir = Path(settings.style_corpus_path)
    if style_dir.exists():
        style_files = list(style_dir.glob("*.md")) + list(style_dir.glob("*.txt"))
        if style_files:
            task_parts.append(
                f"\nStyle corpus available at {style_dir} ({len(style_files)} references)"
            )

    task = "\n".join(task_parts)

    result = asyncio.run(
        run_session(task, session_id=session_id, persistent=not no_persist)
    )
    print_result(result)


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="Task for the agent")],
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", "-s", help="Session identifier"),
    ] = None,
    no_persist: Annotated[
        bool,
        typer.Option("--no-persist", help="Run as one-shot (no sleep/wake loop)"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
) -> None:
    """Run the agent with a freeform task (advanced)."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    result = asyncio.run(
        run_session(task, session_id=session_id, persistent=not no_persist)
    )
    print_result(result)


@app.command()
def loop(
    tasks: Annotated[list[str], typer.Argument(help="Tasks to process")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
    auto_commit: Annotated[
        bool,
        typer.Option("--commit/--no-commit", help="Auto-commit results"),
    ] = True,
) -> None:
    """Run multiple writing sessions in sequence."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    results: list[AgentSessionResult] = []
    total_cost = 0.0

    for i, task in enumerate(tasks, 1):
        typer.echo(f"\n{'=' * 60}")
        typer.echo(f"Task {i}/{len(tasks)}: {task[:80]}")
        typer.echo(f"{'=' * 60}")

        try:
            result = asyncio.run(run_session(task))
            results.append(result)
            total_cost += result.cost_usd or 0
            print_result(result)
        except RuntimeError as e:
            typer.echo(f"Error: {e}", err=True)
            continue
        except Exception as e:
            typer.echo(f"Unexpected error: {e}", err=True)
            logger.exception("Unexpected error on task %d/%d", i, len(tasks))
            continue

        if auto_commit:
            commit_results()

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"Completed {len(results)}/{len(tasks)} sessions")
    typer.echo(f"Total cost: ${total_cost:.4f}")


@app.command()
def setup() -> None:
    """Run the setup wizard to configure integrations.

    Shortcut for `lup-devtools setup`.
    """
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
    style_dir = Path(settings.style_corpus_path)
    style_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source).expanduser()
    if source_path.exists():
        content = source_path.read_text(encoding="utf-8")
        dest = style_dir / source_path.name
        dest.write_text(content, encoding="utf-8")
        typer.echo(f"Added {source_path.name} to style corpus ({len(content)} chars)")
    else:
        refs_file = style_dir / "urls.txt"
        existing = refs_file.read_text(encoding="utf-8") if refs_file.exists() else ""
        if source not in existing:
            with refs_file.open("a", encoding="utf-8") as f:
                f.write(source + "\n")
            typer.echo(f"Added URL to style corpus: {source}")
        else:
            typer.echo(f"URL already in style corpus: {source}")


@style_app.command("list")
def style_list() -> None:
    """List the current style corpus."""
    style_dir = Path(settings.style_corpus_path)
    if not style_dir.exists():
        typer.echo("No style corpus configured. Use `inkwell style add` to start.")
        return

    files = list(style_dir.glob("*.md")) + list(style_dir.glob("*.txt"))
    if not files:
        typer.echo("Style corpus is empty.")
        return

    typer.echo("Style corpus:")
    for f in sorted(files):
        if f.name == "urls.txt":
            urls = f.read_text(encoding="utf-8").strip().split("\n")
            for url in urls:
                if url.strip():
                    typer.echo(f"  [url] {url.strip()}")
        else:
            size = f.stat().st_size
            typer.echo(f"  [file] {f.name} ({size:,} bytes)")


if __name__ == "__main__":
    app()
