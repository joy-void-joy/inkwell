"""Operator commands for the research corpus.

The corpus has to exist before a question does, which makes building it operator
work rather than something a run does for itself: ``corpus sync`` is what
populates it, out of band and on whatever schedule the author likes. A writing
run then reads what is there, and tops up a single source only when it finds one
thin mid-question.

Every command prints what happened per source, because a bulk crawl is only
maintainable if a run tells you which sources are healthy and which need a
declaration looked at.

    uv run lup-devtools corpus sources
    uv run lup-devtools corpus sync
    uv run lup-devtools corpus sync epoch metr --limit 20
    uv run lup-devtools corpus status
    uv run lup-devtools corpus tags
    uv run lup-devtools corpus retag
"""

import asyncio
import logging

import typer

from inkwell.agent.config import corpus_root
from inkwell.corpus.ingest import (
    DEFAULT_CONCURRENCY,
    IngestReport,
    UnknownSource,
    resolve_sources,
    sync_corpus,
)
from inkwell.corpus.registry import (
    DECLARED_SOURCES,
    DROPPED_SOURCES,
    corpus_vocabulary,
)
from inkwell.corpus.storage import CorpusStore
from inkwell.corpus.tagging import retag_corpus

logger = logging.getLogger(__name__)

app = typer.Typer(help="Build and inspect the research corpus", no_args_is_help=True)


def store() -> CorpusStore:
    return CorpusStore(root=corpus_root())


@app.command("sources")
def sources() -> None:
    """List every declared source, and what the corpus deliberately omits."""
    typer.echo(f"corpus root: {corpus_root()}\n")
    typer.echo(f"{'key':<12} {'authority':<13} {'state':<9} name")
    for declaration in DECLARED_SOURCES:
        state = "active" if declaration.active else "inactive"
        typer.echo(
            f"{declaration.key:<12} {declaration.authority:<13} {state:<9} "
            f"{declaration.display_name}"
        )
    typer.echo("\nnot corpus sources:")
    for dropped in DROPPED_SOURCES:
        typer.echo(f"  {dropped.key:<16} {dropped.reason}")


@app.command("status")
def status() -> None:
    """Report what the corpus currently holds, per source."""
    corpus = store()
    typer.echo(f"corpus root: {corpus.root}\n")
    typer.echo(
        f"{'key':<12} {'stored':>7} {'listed':>7} {'failed':>7} {'untagged':>9}  updated"
    )
    for declaration in DECLARED_SOURCES:
        shard = corpus.load_by_name(declaration.key)
        if shard is None:
            typer.echo(
                f"{declaration.key:<12} {'-':>7} {'-':>7} {'-':>7} {'-':>9}  never synced"
            )
            continue
        listed = sum(1 for entry in shard.discovered if entry.present)
        flags = " DEGRADED" if shard.crawl_degraded else ""
        typer.echo(
            f"{declaration.key:<12} {len(shard.documents):>7} {listed:>7} "
            f"{len(shard.failures):>7} {len(shard.untagged()):>9}  "
            f"{shard.updated_at or 'unknown'}{flags}"
        )


@app.command("tags")
def tags() -> None:
    """List the declared tag vocabulary, and what the corpus is tagged with."""
    vocabulary = corpus_vocabulary()
    corpus = store()
    held = {
        tag
        for declaration in DECLARED_SOURCES
        if (shard := corpus.load_by_name(declaration.key)) is not None
        for tag in shard.tags()
    }
    typer.echo(f"vocabulary {vocabulary.signature()}\n")
    for term in vocabulary.terms():
        mark = "in use" if term.tag in held else "unused"
        typer.echo(f"{term.tag:<38} {mark:<7} {term.description}")


@app.command("retag")
def retag(
    source: list[str] = typer.Argument(
        default=None, help="Sources to re-tag (default: every source with an index)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-judge documents already tagged against this exact vocabulary",
    ),
    concurrency: int = typer.Option(
        DEFAULT_CONCURRENCY, "--concurrency", help="Documents judged at once per source"
    ),
) -> None:
    """Re-tag what is stored against the vocabulary as it now stands.

    Fetches nothing: a vocabulary edit costs a reading of the corpus already on
    disk, which is what makes editing it a reasonable thing to do.
    """
    corpus = store()
    keys = tuple(source or ())
    try:
        declarations = (
            resolve_sources(keys)
            if keys
            else tuple(
                declaration
                for declaration in DECLARED_SOURCES
                if declaration.key in corpus.sources()
            )
        )
    except UnknownSource as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(f"vocabulary {corpus_vocabulary().signature()}\n")
    reports = asyncio.run(
        retag_corpus(declarations, corpus, force=force, concurrency=concurrency)
    )
    for report in reports:
        typer.echo(report.summary())


def report_failures(report: IngestReport) -> None:
    """Print the failures a run recorded, so the next run has a target."""
    for source in report.sources:
        for avenue in source.avenue_failures:
            typer.echo(
                f"  {source.source}: avenue {avenue.category or 'sweep'} "
                f"({avenue.origin}) — {avenue.error}"
            )
        for failed in source.failures:
            typer.echo(
                f"  {source.source}/{failed.slug} [{failed.failure_class}] {failed.error}"
            )


@app.command("sync")
def sync(
    source: list[str] = typer.Argument(
        default=None, help="Sources to sync (default: every active source)"
    ),
    limit: int = typer.Option(
        0, "--limit", help="Fetch at most this many new documents per source"
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Refetch documents already stored; the content hash decides what changed",
    ),
    concurrency: int = typer.Option(
        DEFAULT_CONCURRENCY,
        "--concurrency",
        help="Documents fetched at once per source",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Profile whose browser context reaches JS-hard sources"
    ),
) -> None:
    """Enumerate the sources and store what the corpus does not already hold."""
    keys = tuple(source or ())
    try:
        report = asyncio.run(
            sync_corpus(
                store(),
                keys=keys,
                profile=profile,
                concurrency=concurrency,
                limit=limit,
                refresh=refresh,
            )
        )
    except UnknownSource as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(report.summary())
    if report.failed() or any(s.avenue_failures for s in report.sources):
        typer.echo("\nrecorded for the next run to retry:")
        report_failures(report)
    if report.aborted_sources():
        raise typer.Exit(code=1)
