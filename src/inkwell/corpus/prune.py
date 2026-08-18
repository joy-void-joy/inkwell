"""Collapsing what a source served twice, after the fact.

Ingestion recognises a second copy as it arrives and records an alias rather
than a document. This is that same decision applied to a corpus built before
anything was watching: the bytes are already on disk and their hashes are
already in the index, so what a sweep would have decided is recoverable
without refetching a single page.

Dropping is the other half, and it is deliberately not the same operation. A
duplicate is a document the corpus holds twice and can collapse; a document
that is *wrong* — page chrome where an article should be, a sidebar extracted
because the body never rendered — holds nothing worth keeping under any slug,
and the only repair is to fetch it again. So a drop removes the document and
leaves its discovered entry standing, which is precisely the state the next
sweep reads as work to do.
"""

import logging
from itertools import groupby

from pydantic import BaseModel, ConfigDict

from inkwell.corpus.storage import (
    CorpusStore,
    DocumentAlias,
    SourceShard,
    StoredDocument,
    now_stamp,
)

logger = logging.getLogger(__name__)


class PruneReport(BaseModel):
    """What pruning one source did, in the terms an operator asks in."""

    model_config = ConfigDict(frozen=True)

    source: str
    documents: int = 0
    collapsed: int = 0
    dropped: int = 0
    missing: tuple[str, ...] = ()

    def changed(self) -> bool:
        """Whether anything about this source's index would be rewritten."""
        return bool(self.collapsed or self.dropped)

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        counted = " | ".join(
            f"{name} {value}"
            for name, value in (
                ("documents", self.documents),
                ("collapsed", self.collapsed),
                ("dropped", self.dropped),
            )
        )
        tail = f" [no such slug: {', '.join(self.missing)}]" if self.missing else ""
        return f"{self.source}: {counted}{tail}"


def duplicate_sets(shard: SourceShard) -> list[tuple[StoredDocument, ...]]:
    """Every set of documents sharing content, the keeper first.

    The keeper is whichever arrived first, ties broken by slug, so pruning the
    same corpus twice makes the same choice both times. Which one keeps the
    bytes matters less than it looks — they are byte-identical — and the ones
    that do not become aliases naming the one that does.
    """
    digested = sorted(
        (one for one in shard.documents if one.content_sha256),
        key=lambda one: (one.content_sha256, one.fetched_at, one.slug),
    )
    grouped = (
        tuple(group)
        for _, group in groupby(digested, key=lambda one: one.content_sha256)
    )
    return [group for group in grouped if len(group) > 1]


def prune_source(
    source: str,
    store: CorpusStore,
    *,
    drop: tuple[str, ...] = (),
    dry_run: bool = False,
) -> PruneReport:
    """Collapse one source's duplicates, and drop the slugs named.

    Reads and writes disk only, so this costs no fetch and no judgement. A dry
    run reports exactly what a real one would do and touches nothing, because
    the point of a destructive operation is being able to see it first.

    Dropping a document releases the aliases that named it. An alias says "the
    bytes are over there", so one whose holder has just been removed points at
    nothing and would keep its own URL settled against a document the corpus
    no longer has — which is exactly the URL a repair wants fetched again.
    """
    shard = store.load_by_name(source)
    if shard is None:
        return PruneReport(source=source)

    held = {document.slug for document in shard.documents}
    dropping = [one for one in shard.documents if one.slug in drop]
    collapsing = [
        (keeper, copy)
        for keeper, *rest in duplicate_sets(shard)
        for copy in rest
        if copy.slug not in drop
    ]

    report = PruneReport(
        source=source,
        documents=len(shard.documents),
        collapsed=len(collapsing),
        dropped=len(dropping),
        missing=tuple(slug for slug in drop if slug not in held),
    )
    if dry_run or not report.changed():
        return report

    for keeper, copy in collapsing:
        shard.record_alias(
            DocumentAlias(
                slug=copy.slug,
                url=copy.url,
                holder=keeper.slug,
                content_sha256=copy.content_sha256,
                seen_at=now_stamp(),
            )
        )
    leaving = [*dropping, *(copy for _, copy in collapsing)]
    for document in leaving:
        store.remove_document(source, document)
    gone = {one.slug for one in leaving}
    shard.documents = [one for one in shard.documents if one.slug not in gone]
    shard.aliases = [
        alias
        for alias in shard.aliases
        if alias.holder not in gone and alias.slug not in drop
    ]

    store.save(shard)
    logger.info("Pruned %s\n%s", source, report.summary())
    return report


def prune_corpus(
    sources: tuple[str, ...],
    store: CorpusStore,
    *,
    drop: tuple[str, ...] = (),
    dry_run: bool = False,
) -> tuple[PruneReport, ...]:
    """Prune several sources in turn, reporting each."""
    return tuple(
        prune_source(source, store, drop=drop, dry_run=dry_run) for source in sources
    )
