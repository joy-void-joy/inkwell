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
    uv run lup-devtools corpus progress --watch 30
    uv run lup-devtools corpus tags
    uv run lup-devtools corpus retag
    uv run lup-devtools corpus embed
"""

import asyncio
import logging
import sys
from collections.abc import Coroutine
from shutil import get_terminal_size
from time import monotonic, sleep
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict, Field
from tqdm import tqdm

from inkwell.agent.config import corpus_root, current_settings
from inkwell.agent.pipeline import CORPUS_BRIEFING_LIMIT, corpus_briefing
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
    active_declarations,
    corpus_vocabulary,
)
from inkwell.corpus.semantics import LocalEmbedder, embed_source
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


@app.command("brief")
def brief(
    topic: Annotated[str, typer.Argument(help="Subject to brief the planner on")],
    limit: Annotated[
        int, typer.Option(help="How many documents to show")
    ] = CORPUS_BRIEFING_LIMIT,
) -> None:
    """Show the corpus briefing the plan stage would be handed for a topic.

    The planner is given this before it writes a research question, so what it
    says here is what a run can notice that its source material never raised.
    Seeing it costs nothing and answers the question a sync cannot: not "what
    did we store" but "what will the planner actually be shown".
    """
    rendered = asyncio.run(corpus_briefing(topic, limit=limit))
    typer.echo(rendered or "The corpus has nothing on this subject.")


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


BAR_WIDTH = 24
"""How wide a progress bar is drawn, in characters."""

PROGRESS_INTERVAL = 2.0
"""Seconds between readings of the store while a sweep runs.

Short enough that the bar moves, long enough that reading every shard is a
rounding error against fetching a document.
"""

FALLBACK_WIDTH = 80
"""How wide to draw when the terminal will not say.

Given to tqdm rather than left to it. Asked to work its own width out, tqdm
reads the window size directly, and a terminal reporting zero — a pty opened
without one, which is what a scripted or captured run gets — has it draw a bar
zero characters wide. That renders as nothing at all, which is
indistinguishable from the display being switched off.
"""


class SweepProgress(BaseModel):
    """How far a sweep has got, counted off the store rather than the run.

    Read from disk on purpose. ``corpus sync`` reports per source when the
    whole run finishes, so a sweep of a dozen sources says nothing for as long
    as it takes — but each source publishes its shard as it goes, so what has
    landed is on disk and can be counted by anything, including after the
    process that was doing it has died.
    """

    model_config = ConfigDict(frozen=True)

    stored: int = Field(description="Documents the corpus holds")
    listed: int = Field(description="Documents enumeration found to fetch")
    started: int = Field(description="Sources that have published a shard")
    total: int = Field(description="Sources a full sweep would touch")

    def fraction(self) -> float:
        """How much of the enumerated work is done, 0 where nothing is listed."""
        return self.stored / self.listed if self.listed else 0.0

    def bar(self) -> str:
        """The fraction as a bar somebody watching can read at a glance."""
        filled = round(self.fraction() * BAR_WIDTH)
        return "#" * filled + "." * (BAR_WIDTH - filled)

    def eta(self, elapsed: float, gained: int) -> float:
        """Seconds until the enumerated documents are all fetched.

        From the rate this watch has actually seen, which is why it needs
        ``gained`` rather than working from :attr:`stored`. A sweep almost
        never starts from an empty corpus — the store already holds what every
        earlier run left — so pacing against the total credits this run with
        documents it did not fetch, and the first reading, taken a fraction of
        a second in, reports a rate in the hundreds of thousands.

        It is an estimate over a moving denominator — enumeration keeps
        finding documents while fetching runs — so it moves, and a caller
        showing it should say so rather than presenting it as a countdown.
        """
        if gained <= 0 or elapsed <= 0:
            return 0.0
        return (self.listed - self.stored) * elapsed / gained

    def rate(self, elapsed: float, gained: int) -> float:
        """Documents a minute, over what this watch has actually seen land."""
        return gained * 60 / elapsed if elapsed > 0 and gained > 0 else 0.0

    def render(self, elapsed: float, gained: int = 0) -> str:
        """One line: the bar, the counts, the rate, and where it is heading.

        ``gained`` is what has landed since this watch began. A single
        reading — the bare ``progress`` command — has watched for no time and
        seen nothing land, so it reports no rate rather than a made-up one.
        """
        remaining = self.eta(elapsed, gained)
        pace = f"{self.rate(elapsed, gained):.0f}/min · " if elapsed > 0 else ""
        heading = f" · eta ~{tqdm.format_interval(remaining)}" if remaining > 0 else ""
        return (
            f"[{self.bar()}] {self.stored}/{self.listed} docs "
            f"({self.fraction():.0%}) · {self.started}/{self.total} sources · "
            f"{pace}{tqdm.format_interval(elapsed)} elapsed{heading}"
        )


def sweep_bar(opening: SweepProgress) -> tqdm | None:
    """A bar to draw the sweep in, or nothing where one could not be seen.

    ``initial`` is what the store already held, and it is the whole reason the
    rate and the time remaining are honest. A sweep almost never starts from an
    empty corpus, and tqdm paces on ``n - initial``, so telling it where this
    run began is what stops it crediting the run with every document each
    earlier run fetched — which is how the first reading came out at nearly a
    million a minute.

    ``None`` where stderr is not a terminal. A redrawing bar needs one to
    redraw over; piped to a file or captured by another program it is not a
    display but a few hundred kilobytes of escape codes, and the caller says
    the same thing in lines instead. What it must never do is what it did
    before: decide it cannot draw, and then say nothing at all.
    """
    if not sys.stderr.isatty():
        return None
    return tqdm(
        total=opening.listed or None,
        initial=opening.stored,
        unit="doc",
        desc="sweeping",
        leave=True,
        ncols=get_terminal_size(fallback=(FALLBACK_WIDTH, 24)).columns
        or FALLBACK_WIDTH,
    )


async def tracked(
    corpus: CorpusStore, interval: float, showing: bool, until: asyncio.Event
) -> None:
    """Say how far a sweep has got, until told to stop.

    Counted off the store rather than off the run, exactly as ``progress``
    does — a sweep publishes each shard as it goes, so what has landed is on
    disk and needs no channel back from the fetching. That also means this
    reports honestly through an interrupt: what it last said is where the
    corpus actually got to.

    A bar in a terminal and a line everywhere else, because a redrawing bar
    needs a terminal to redraw over and a log full of escape codes is not a
    progress display. Both say the same numbers; only the two ways of failing
    are gone. The bar no longer switches itself off silently, and no longer
    draws itself zero characters wide.

    Stopped by an event rather than by cancellation, so ending it raises
    nothing that has to be caught and discarded, and so the last reading is
    taken *after* the sweep finished rather than whenever the poll last came
    round. The cost is that this can sit up to one interval past the end,
    which against a sweep measured in hours is not a cost.

    The total moves. Enumeration keeps finding documents while fetching runs,
    so the denominator grows and the bar can lose ground in percentage terms
    while gaining it in documents. Better that than a fixed total that was a
    guess.
    """
    if not showing:
        return
    started = monotonic()
    opening = swept(corpus)
    bar = sweep_bar(opening)
    spoken = -1

    def drawn() -> None:
        """One reading of the store, rendered however this run can be watched.

        The line is emitted only when the count moves, so a sweep waiting on a
        slow source stays quiet instead of repeating itself and every line that
        appears marks a document that landed. The bar redraws regardless, since
        its elapsed time and its estimate move whether or not anything did.
        """
        nonlocal spoken
        found = swept(corpus)
        if bar is not None:
            bar.total = found.listed or None
            bar.n = found.stored
            bar.set_postfix_str(f"{found.started}/{found.total} sources")
            bar.refresh()
            return
        if found.stored != spoken:
            spoken = found.stored
            typer.echo(
                found.render(monotonic() - started, found.stored - opening.stored),
                err=True,
            )

    try:
        while not until.is_set():
            drawn()
            await asyncio.sleep(interval)
        drawn()
    finally:
        if bar is not None:
            bar.close()


async def swept_with_bar(
    corpus: CorpusStore, sweep: Coroutine[None, None, IngestReport], showing: bool
) -> IngestReport:
    """Run one sweep with a bar beside it, and stop the bar however it ends.

    The bar is stopped in a ``finally`` so a sweep that raises still leaves
    the terminal in one piece and still shows where the corpus got to.
    """
    until = asyncio.Event()
    watching = asyncio.create_task(tracked(corpus, PROGRESS_INTERVAL, showing, until))
    try:
        return await sweep
    finally:
        until.set()
        await watching


def swept(corpus: CorpusStore) -> SweepProgress:
    """What the store holds right now, across every active source."""
    active = active_declarations()
    shards = [
        shard
        for declaration in active
        if (shard := corpus.load_by_name(declaration.key)) is not None
    ]
    return SweepProgress(
        stored=sum(len(shard.documents) for shard in shards),
        listed=sum(
            1 for shard in shards for entry in shard.discovered if entry.present
        ),
        started=len(shards),
        total=len(active),
    )


@app.command("progress")
def progress(
    watch: float = typer.Option(
        0.0, "--watch", help="Keep reporting every this many seconds; 0 reports once"
    ),
) -> None:
    """Say how far a sweep has got, while it is still going.

    ``sync`` prints its per-source report when the whole run ends, which for a
    dozen sources is a long silence. This reads the same shards the sweep is
    writing, so it answers from outside the run — including from another
    terminal, or after the run was interrupted.
    """
    corpus = store()
    started = monotonic()
    opening = swept(corpus)
    typer.echo(opening.render(0.0))
    if watch <= 0:
        return
    while True:
        sleep(watch)
        elapsed = monotonic() - started
        found = swept(corpus)
        typer.echo(found.render(elapsed, found.stored - opening.stored), nl=True)
        if found.listed and found.stored >= found.listed:
            typer.echo("sweep has fetched everything enumerated")
            return


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


@app.command("embed")
def embed(
    source: list[str] = typer.Argument(
        default=None, help="Sources to embed (default: every source with an index)"
    ),
    model: str = typer.Option(
        "", "--model", help="Embedding model to use (default: the configured one)"
    ),
) -> None:
    """Compute the optional semantic layer's vectors, one file per source.

    Nothing else needs this to have been run: browsing, filtering, and every
    ordering are structural and answer with no vectors at all. What this buys
    is the question that shares no vocabulary with the corpus, and it is worth
    running after a sync rather than before one.
    """
    corpus = store()
    keys = tuple(source or corpus.sources())
    if not keys:
        typer.echo("Nothing is ingested yet — run `corpus sync` first.")
        raise typer.Exit(code=1)

    embedder = LocalEmbedder(model or current_settings().corpus_embedding_model)
    typer.echo(f"embedding with {embedder.identity()}\n")
    for key in keys:
        report = asyncio.run(embed_source(key, corpus, embedder))
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
    show_progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Say how far the sweep has got as it goes — a bar in a terminal, "
        "a line per document elsewhere",
    ),
) -> None:
    """Enumerate the sources and store what the corpus does not already hold.

    The per-source report comes at the end, because a source is only fully
    accounted for once its run finishes. The bar comes throughout, so a sweep
    of a dozen sources over several hours is not a silent one — it reads the
    shards the sweep is publishing, which is the same thing ``corpus
    progress`` reads from another terminal and works the same way here.
    """
    keys = tuple(source or ())
    corpus = store()
    try:
        report = asyncio.run(
            swept_with_bar(
                corpus,
                sync_corpus(
                    corpus,
                    keys=keys,
                    profile=profile,
                    concurrency=concurrency,
                    limit=limit,
                    refresh=refresh,
                ),
                show_progress,
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
