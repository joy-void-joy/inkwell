# Generated from inkwell.environment.cli.compile by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

"""One typer command per declared writing entry point.

Each command's parameters, help text, flags, and defaults come from the entry
point declaration in :mod:`inkwell.environment.entrypoints`; the body collects
them under their declared names and hands them to the one launch path this CLI
and the web API both run through. Adding a parameter to a declaration and
regenerating is the whole of the change here.
"""

from typing import Annotated

import typer


def write(
    sources: Annotated[
        list[str] | None,
        typer.Argument(
            help="Source materials: Claude share links, URLs, file paths, or freeform text",
        ),
    ] = None,
    refs: Annotated[
        list[str] | None,
        typer.Option(
            "--ref",
            "-r",
            help="Supplementary reference URLs or file paths",
        ),
    ] = None,
    target_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Suggested format (the agent may override): academic, lesswrong, textbook, blog, twitter, dialog, memo, newsletter, linkedin, custom:<description>",
        ),
    ] = "auto",
    chapter: Annotated[
        str | None,
        typer.Option(
            "--chapter",
            help="Which chapter of which book this run writes, so it reads what the book's other chapters recorded and files its own record beside them. Leave it unset for a standalone piece, which belongs to no book. Spelled book:chapter, as in 'atlas:4'.",
        ),
    ] = None,
    existing_doc_id: Annotated[
        str | None,
        typer.Option(
            "--doc",
            "-d",
            help="Google Doc URL or id to write into, instead of creating a new document",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Pause once this stage finishes and exit cleanly — review and comment in the Doc, then resume the session to continue. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    light: Annotated[
        bool,
        typer.Option(
            "--light",
            help="Run the light pipeline: a single writer, fact-check-only review, and no deep research, resolve, or rewrite. Implied by the LinkedIn format.",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            "-s",
            help="Session identifier to run under, instead of a freshly minted one",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model for every stage, overriding the profile's default",
        ),
    ] = None,
    writer_mode: Annotated[
        str | None,
        typer.Option(
            "--writer-mode",
            help="Draft production mode: 'parallel' (one writer per section, then merge) or 'single' (one writer drafts the whole piece)",
        ),
    ] = None,
    stage_models: Annotated[
        list[str] | None,
        typer.Option(
            "--stage-model",
            help="Per-stage model override, as stage=model; repeat the option per stage",
        ),
    ] = None,
) -> None:
    """Write an article from source material.

    Each source can be a Claude.ai share link, a URL to extract content from, a
    local file path, or a writing brief. Several sources are extracted and combined
    for the pipeline.

    Examples:
        inkwell write "https://claude.ai/share/abc123"
        inkwell write "https://claude.ai/share/abc123" paper.pdf -f twitter
        inkwell write conversation.md --stop-after plan   # pause to review the plan
    """
    from inkwell.environment.cli.chat import run_entry_point

    run_entry_point(
        "write",
        {
            "sources": sources,
            "refs": refs,
            "target_format": target_format,
            "chapter": chapter,
            "existing_doc_id": existing_doc_id,
            "stop_after": stop_after,
            "light": light,
            "session_id": session_id,
            "verbose": verbose,
            "model": model,
            "writer_mode": writer_mode,
            "stage_models": stage_models,
        },
    )


def run(
    task: Annotated[
        str,
        typer.Argument(
            help="Freeform task for the agent — what to write, in your own words",
        ),
    ],
    target_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Suggested format (the agent may override): academic, lesswrong, textbook, blog, twitter, dialog, memo, newsletter, linkedin, custom:<description>",
        ),
    ] = "auto",
    chapter: Annotated[
        str | None,
        typer.Option(
            "--chapter",
            help="Which chapter of which book this run writes, so it reads what the book's other chapters recorded and files its own record beside them. Leave it unset for a standalone piece, which belongs to no book. Spelled book:chapter, as in 'atlas:4'.",
        ),
    ] = None,
    existing_doc_id: Annotated[
        str | None,
        typer.Option(
            "--doc",
            "-d",
            help="Google Doc URL or id to write into, instead of creating a new document",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Pause once this stage finishes and exit cleanly — review and comment in the Doc, then resume the session to continue. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    light: Annotated[
        bool,
        typer.Option(
            "--light",
            help="Run the light pipeline: a single writer, fact-check-only review, and no deep research, resolve, or rewrite. Implied by the LinkedIn format.",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            "-s",
            help="Session identifier to run under, instead of a freshly minted one",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model for every stage, overriding the profile's default",
        ),
    ] = None,
    writer_mode: Annotated[
        str | None,
        typer.Option(
            "--writer-mode",
            help="Draft production mode: 'parallel' (one writer per section, then merge) or 'single' (one writer drafts the whole piece)",
        ),
    ] = None,
    stage_models: Annotated[
        list[str] | None,
        typer.Option(
            "--stage-model",
            help="Per-stage model override, as stage=model; repeat the option per stage",
        ),
    ] = None,
) -> None:
    """Write from a freeform task rather than from source links.

    The task text is the planner's source material: it extracts a structure and runs
    the ordinary pipeline. For source links and files, use `inkwell write`.

    Examples:
        inkwell run "write a blog post about tokenizer economics"
    """
    from inkwell.environment.cli.chat import run_entry_point

    run_entry_point(
        "run",
        {
            "task": task,
            "target_format": target_format,
            "chapter": chapter,
            "existing_doc_id": existing_doc_id,
            "stop_after": stop_after,
            "light": light,
            "session_id": session_id,
            "verbose": verbose,
            "model": model,
            "writer_mode": writer_mode,
            "stage_models": stage_models,
        },
    )


def revise(
    draft: Annotated[
        str,
        typer.Argument(
            help="The section to revise: a Google Doc URL, a file path, a URL, or the draft text itself",
        ),
    ],
    refs: Annotated[
        list[str] | None,
        typer.Option(
            "--ref",
            "-r",
            help="Supplementary reference URLs or file paths",
        ),
    ] = None,
    target_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Suggested format (the agent may override): academic, lesswrong, textbook, blog, twitter, dialog, memo, newsletter, linkedin, custom:<description>",
        ),
    ] = "auto",
    chapter: Annotated[
        str | None,
        typer.Option(
            "--chapter",
            help="Which chapter of which book this run writes, so it reads what the book's other chapters recorded and files its own record beside them. Leave it unset for a standalone piece, which belongs to no book. Spelled book:chapter, as in 'atlas:4'.",
        ),
    ] = None,
    existing_doc_id: Annotated[
        str | None,
        typer.Option(
            "--doc",
            "-d",
            help="Google Doc URL or id to write into, instead of creating a new document",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Pause once this stage finishes and exit cleanly — review and comment in the Doc, then resume the session to continue. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    light: Annotated[
        bool,
        typer.Option(
            "--light",
            help="Run the light pipeline: a single writer, fact-check-only review, and no deep research, resolve, or rewrite. Implied by the LinkedIn format.",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            "-s",
            help="Session identifier to run under, instead of a freshly minted one",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model for every stage, overriding the profile's default",
        ),
    ] = None,
    writer_mode: Annotated[
        str | None,
        typer.Option(
            "--writer-mode",
            help="Draft production mode: 'parallel' (one writer per section, then merge) or 'single' (one writer drafts the whole piece)",
        ),
    ] = None,
    stage_models: Annotated[
        list[str] | None,
        typer.Option(
            "--stage-model",
            help="Per-stage model override, as stage=model; repeat the option per stage",
        ),
    ] = None,
) -> None:
    """Revise an existing draft for clarity and currency.

    Takes a section you already have — a Doc, a file, a URL, or the text itself —
    and runs it through the ordinary writing pipeline as the source, under a
    standing instruction to rewrite it for clarity and currency without changing its
    voice or its argument.

    Examples:
        inkwell revise draft.md
        inkwell revise chapter4.md --chapter atlas:4   # one chapter of a book
        inkwell revise "https://docs.google.com/document/d/abc123/edit"
    """
    from inkwell.environment.cli.chat import run_entry_point

    run_entry_point(
        "revise",
        {
            "draft": draft,
            "refs": refs,
            "target_format": target_format,
            "chapter": chapter,
            "existing_doc_id": existing_doc_id,
            "stop_after": stop_after,
            "light": light,
            "session_id": session_id,
            "verbose": verbose,
            "model": model,
            "writer_mode": writer_mode,
            "stage_models": stage_models,
        },
    )


def resume(
    resumed_session: Annotated[
        str,
        typer.Argument(
            help="Session id to continue (see `inkwell sessions`)",
        ),
    ],
    from_stage: Annotated[
        str | None,
        typer.Option(
            "--from",
            "-f",
            help="Pick up after this stage instead of where the run stopped. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Pause once this stage finishes and exit cleanly — review and comment in the Doc, then resume the session to continue. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model for every stage, overriding the profile's default",
        ),
    ] = None,
    writer_mode: Annotated[
        str | None,
        typer.Option(
            "--writer-mode",
            help="Draft production mode: 'parallel' (one writer per section, then merge) or 'single' (one writer drafts the whole piece)",
        ),
    ] = None,
    stage_models: Annotated[
        list[str] | None,
        typer.Option(
            "--stage-model",
            help="Per-stage model override, as stage=model; repeat the option per stage",
        ),
    ] = None,
) -> None:
    """Resume a previous writing session from its saved pipeline state.

    By default picks up from the last completed stage. Use --from to resume from an
    earlier checkpoint, and --stop-after to pause again at a later one.

    Examples:
        inkwell sessions                              # find the session id
        inkwell resume 20260523_143022                # resume from the last stage
        inkwell resume 20260523_143022 --from write   # re-run merge onward
    """
    from inkwell.environment.cli.chat import run_entry_point

    run_entry_point(
        "resume",
        {
            "resumed_session": resumed_session,
            "from_stage": from_stage,
            "stop_after": stop_after,
            "verbose": verbose,
            "model": model,
            "writer_mode": writer_mode,
            "stage_models": stage_models,
        },
    )


def restart(
    resumed_session: Annotated[
        str,
        typer.Argument(
            help="Session id to continue (see `inkwell sessions`)",
        ),
    ],
    from_stage: Annotated[
        str,
        typer.Option(
            "--from",
            "-f",
            help="Re-run this stage and everything after it from scratch with fresh agents, discarding their prior output. Stages: extract, voice, plan, research, assumptions, refine, write, merge, review, rewrite, format",
        ),
    ],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model for every stage, overriding the profile's default",
        ),
    ] = None,
    writer_mode: Annotated[
        str | None,
        typer.Option(
            "--writer-mode",
            help="Draft production mode: 'parallel' (one writer per section, then merge) or 'single' (one writer drafts the whole piece)",
        ),
    ] = None,
    stage_models: Annotated[
        list[str] | None,
        typer.Option(
            "--stage-model",
            help="Per-stage model override, as stage=model; repeat the option per stage",
        ),
    ] = None,
) -> None:
    """Re-run a stage of a saved session from scratch with fresh agents.

    Unlike resume, which continues an interrupted agent's own conversation, restart
    rewinds to the checkpoint before the named stage and regenerates that stage and
    everything after it, discarding their prior output.

    Examples:
        inkwell restart 20260523_143022 --from write
    """
    from inkwell.environment.cli.chat import run_entry_point

    run_entry_point(
        "restart",
        {
            "resumed_session": resumed_session,
            "from_stage": from_stage,
            "verbose": verbose,
            "model": model,
            "writer_mode": writer_mode,
            "stage_models": stage_models,
        },
    )


def register(app: typer.Typer) -> None:
    """Add every declared entry point to ``app`` as its own command."""
    app.command("write")(write)
    app.command("run")(run)
    app.command("revise")(revise)
    app.command("resume")(resume)
    app.command("restart")(restart)
