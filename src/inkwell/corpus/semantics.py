"""The optional semantic layer: vectors beside the index, and who computes them.

Optional is the design constraint, not a caveat on it. Everything the corpus is
browsed by — titles, tags, venue, date, source — is structural, and answers
with no vector anywhere on disk. What a vector adds is the one question
structure cannot be asked: *what else is like this*, for a reader whose wording
the corpus never used. A corpus with no vectors answers every other question
and reports this layer absent; it does not fail, and it does not quietly return
nothing, which is the failure that would teach an agent the corpus is empty.

Who computes a vector is a seam rather than a decision baked in here. The
implementation that ships is local, so switching the layer on costs an install
rather than an API key and a network round trip per query; an API-backed
embedder is a second class behind the same seam rather than an edit to anything
that calls one.

Vectors live in one file per source beside that source's index —
``<root>/<source>/vectors.json`` — for the same reason the index is sharded:
one source is the unit everything is built and topped up in. Nothing here
starts a process, opens a port, or has to be running for a query to work.
"""

import asyncio
import importlib
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument, now_stamp

logger = logging.getLogger(__name__)

VECTORS_FILENAME = "vectors.json"
"""One file per source, beside that source's ``index.json``."""

MODEL2VEC_MODULE = "model2vec"
"""The library the local embedder loads a static model through, named rather
than imported at module scope — see ``static_encoder``."""

DEFAULT_LOCAL_MODEL = "minishlab/potion-base-8M"
"""Which local model the semantic layer distils vectors with unless configured
otherwise. A static-embedding model: small enough to run on a CPU beside the
rest of a writing run, which is what makes a local default plausible at all."""

EMBEDDINGS_INSTALL = "uv sync --extra embeddings"
"""What to run when the layer is switched on and the library is not there. The
extra is declared, so this installs it rather than adding it — a fresh clone
gets the corpus and none of the embedding machinery until it asks. Said in the
absence report rather than logged, because the reader who needs it is the agent
that just asked for a neighbour and got none."""


class DocumentVector(BaseModel):
    """One document's place in the embedding space."""

    model_config = ConfigDict(frozen=True)

    slug: str
    values: tuple[float, ...]


class VectorShard(BaseModel):
    """One source's vectors, and what computed them.

    ``embedder`` travels with the vectors because two embedders produce
    coordinates in unrelated spaces: comparing across them returns numbers that
    look like similarities and mean nothing, so a shard says which space it is
    in rather than leaving that to whoever reads the file.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    embedder: str = ""
    dimensions: int = 0
    updated_at: str = ""
    vectors: tuple[DocumentVector, ...] = ()

    def by_slug(self) -> dict[str, tuple[float, ...]]:
        return {vector.slug: vector.values for vector in self.vectors}


def vectors_path(store: CorpusStore, source: str) -> Path:
    return store.source_dir(source) / VECTORS_FILENAME


def read_vectors(store: CorpusStore, source: str) -> VectorShard | None:
    """One source's vectors, or None where none have been computed.

    An unreadable file is reported and treated as absent for the same reason an
    unreadable index is: the layer is optional, so its worst failure should
    cost a neighbour query, never a browse.
    """
    path = vectors_path(store, source)
    if not path.is_file():
        return None
    try:
        return VectorShard.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        logger.warning("Unreadable corpus vectors at %s", path, exc_info=True)
        return None


def write_vectors(store: CorpusStore, shard: VectorShard) -> Path:
    """Write one source's vectors, replacing whatever was there."""
    path = vectors_path(store, shard.source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shard.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """How close two vectors point, or 0.0 for anything not comparable.

    Vectors of different lengths came from different models, and a zero vector
    has no direction; neither is worth raising over, because both mean the same
    thing to a caller — this pair says nothing about that pair.
    """
    if len(left) != len(right) or not left:
        return 0.0
    scale = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if not scale:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / scale


def embedded_text(document: StoredDocument) -> str:
    """What of a document a vector is computed from.

    Title, then whatever the narrow tier would show — the abstract the source
    published, or the summary the tagging pass judged where it published none.
    Never the whole body: the layer exists to answer *what is this about*, and
    a hundred pages of appendix pull that answer toward whatever the appendix
    happens to be full of. A PDF contributes no extracted text at all, which is
    exactly why a judged summary is the thing standing in for its abstract.
    """
    return "\n".join(
        part for part in (document.title, document.abstract or document.summary) if part
    )


class CorpusEmbedder(ABC):
    """Who turns a document's text into a vector.

    A seam, only ever injected: nothing here constructs one for itself, so a
    test hands over an embedder that answers from a fixture and a run hands
    over one that loads a model. Adding an API-backed embedder is a class
    beside ``LocalEmbedder``, and no caller changes.
    """

    @abstractmethod
    def identity(self) -> str:
        """What this embedder is, recorded beside the vectors it produced."""

    @abstractmethod
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """One vector per text, in the order the texts arrived."""


type Encoder = Callable[[tuple[str, ...]], list[list[float]]]
"""Text in, vectors out — the whole of what a local model is used for."""


def static_encoder(model: str) -> Encoder:
    """Load ``model`` and return the one call this makes of it.

    Imported by name rather than at module scope, because the library is an
    optional extra and a checkout without it has to load, browse, and filter
    exactly as one with it does. What comes back is narrowed here to text in,
    floats out, so nothing downstream holds a type this project does not own.

    ``force_download`` is off against the library's own default, which re-fetches
    a model already on disk: a cached model is the whole reason a local layer
    costs no network, and a hub path is only one of the two things ``model`` can
    be — a directory holding a saved model is the other, and loads offline.
    """
    module = importlib.import_module(MODEL2VEC_MODULE)
    loaded = module.StaticModel.from_pretrained(model, force_download=False)

    def encode(texts: tuple[str, ...]) -> list[list[float]]:
        return [[float(value) for value in row] for row in loaded.encode(list(texts))]

    return encode


class LocalEmbedder(CorpusEmbedder):
    """Vectors computed in this process, from a small static model on disk.

    Local first because the layer is optional: a corpus nobody has switched
    embeddings on for should cost nothing but an install to switch on, and an
    API key is a cost paid before anyone knows whether the layer earns its
    keep. The model loads when a vector is first asked for and is kept, so a
    run that never asks never pays for it.
    """

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL) -> None:
        self.model = model
        self.encoder: Encoder | None = None

    def identity(self) -> str:
        return f"local:{self.model}"

    def loaded(self) -> Encoder:
        if self.encoder is None:
            self.encoder = static_encoder(self.model)
        return self.encoder

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        rows = await asyncio.to_thread(self.loaded(), texts)
        return tuple(tuple(row) for row in rows)


class SemanticStatus(BaseModel):
    """Whether the semantic layer can answer, and why not where it cannot.

    Absence is reported rather than raised, and reported *specifically*: "not
    switched on", "switched on but nothing is embedded yet", and "switched on
    but the embedder would not load" are three different things to do next, and
    one shared "unavailable" tells a reader none of them.
    """

    model_config = ConfigDict(frozen=True)

    available: bool = Field(
        default=False, description="Whether a nearest-neighbour query can be answered"
    )
    reason: str = Field(
        default="",
        description="Why the layer cannot answer, and what would make it able to",
    )
    embedder: str = Field(
        default="", description="What computed the vectors that are stored"
    )
    embedded: int = Field(
        default=0,
        description="How many documents of the searched sources have a vector",
    )


NOT_ENABLED = (
    "The semantic layer is off, and every browse, filter, and ordering here is "
    "structural and unaffected by that. Switch it on with "
    "INKWELL_CORPUS_EMBEDDINGS=true, then compute vectors with "
    "`lup-devtools corpus embed`."
)

NOTHING_EMBEDDED = (
    "The semantic layer is on, but no vectors are stored for these sources yet "
    "— run `lup-devtools corpus embed`. Structural browsing is unaffected."
)


def load_failure(error: Exception) -> str:
    """What to say when an embedder will not answer, wherever it was asked.

    The install command travels with the error rather than only with the query
    that hit it, because the two places this surfaces — a neighbour query and
    an operator running the embed command — both fail on a missing extra, and
    an operator reading a bare ``ModuleNotFoundError`` has to go and find out
    that the extra is what it means.
    """
    return (
        f"The semantic layer's embedder could not be used "
        f"({type(error).__name__}: {error}). Install it with "
        f"`{EMBEDDINGS_INSTALL}`, or leave the layer off — structural browsing "
        f"is unaffected either way."
    )


class Neighbour(BaseModel):
    """One document the semantic layer places near a question."""

    model_config = ConfigDict(frozen=True)

    source: str
    slug: str
    similarity: float

    def document(self) -> str:
        """How a neighbour names the document it is about, across sources."""
        return f"{self.source}/{self.slug}"


class SemanticAnswer(BaseModel):
    """A neighbour query's result, and whether it could be asked at all.

    The two travel together so that no caller has to tell "nothing in the
    corpus is close to this" apart from "nothing could be asked" by looking at
    an empty list and guessing.
    """

    model_config = ConfigDict(frozen=True)

    status: SemanticStatus
    neighbours: tuple[Neighbour, ...] = ()

    def similarity(self, document: str) -> float:
        """How near ``source/slug`` sits to the question.

        0.0 for a document the layer never placed, which is the same answer it
        gives for one it placed nowhere near — both mean "this result is not
        here because of the semantic layer", which is what a reader needs.
        """
        return next(
            (one.similarity for one in self.neighbours if one.document() == document),
            0.0,
        )


class SemanticLayer(BaseModel):
    """The corpus's optional nearest-neighbour surface.

    A plain class parametrized by which embedder fills the seam, so a consumer
    holds this and never a ``CorpusEmbedder``. Every method answers: where the
    layer cannot, it says so in a ``SemanticStatus`` and the caller carries on
    with the structural result it already has.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: CorpusStore
    enabled: bool = False
    embedder: CorpusEmbedder | None = None

    def shards(self, sources: tuple[str, ...]) -> dict[str, VectorShard]:
        """The vectors stored for each of ``sources`` that has any."""
        found = ((source, read_vectors(self.store, source)) for source in sources)
        return {source: shard for source, shard in found if shard is not None}

    def status(self, sources: tuple[str, ...]) -> SemanticStatus:
        """Whether a neighbour query over ``sources`` could be answered."""
        if not self.enabled:
            return SemanticStatus(reason=NOT_ENABLED)
        held = self.shards(sources)
        embedded = sum(len(shard.vectors) for shard in held.values())
        if not embedded:
            return SemanticStatus(reason=NOTHING_EMBEDDED)
        return SemanticStatus(
            available=True,
            embedder=next(iter(held.values())).embedder,
            embedded=embedded,
        )

    def asking(self) -> CorpusEmbedder:
        """The embedder to ask, built from configuration where none was given."""
        if self.embedder is not None:
            return self.embedder
        from inkwell.agent.config import current_settings

        return LocalEmbedder(current_settings().corpus_embedding_model)

    async def nearest(self, question: str, sources: tuple[str, ...]) -> SemanticAnswer:
        """Order ``sources``' embedded documents by closeness to ``question``."""
        standing = self.status(sources)
        if not standing.available:
            return SemanticAnswer(status=standing)
        try:
            asked = (await self.asking().embed((question,)))[0]
        except Exception as error:
            logger.warning("The semantic layer could not embed a query", exc_info=True)
            return SemanticAnswer(status=SemanticStatus(reason=load_failure(error)))

        held = self.shards(sources)
        return SemanticAnswer(
            status=standing,
            neighbours=tuple(
                sorted(
                    (
                        Neighbour(
                            source=source, slug=slug, similarity=cosine(asked, values)
                        )
                        for source, shard in held.items()
                        for slug, values in shard.by_slug().items()
                    ),
                    key=lambda neighbour: neighbour.similarity,
                    reverse=True,
                )
            ),
        )


class EmbedReport(BaseModel):
    """What embedding one source did, in the terms an operator asks in."""

    model_config = ConfigDict(frozen=True)

    source: str
    embedder: str = ""
    documents: int = 0
    embedded: int = 0
    skipped: int = 0
    failure: str = ""

    def summary(self) -> str:
        """One line naming what happened, for a log or a CLI."""
        if self.failure:
            return f"{self.source}: {self.failure}"
        return (
            f"{self.source}: embedded {self.embedded} of {self.documents} "
            f"| nothing to read {self.skipped} | {self.embedder}"
        )


async def embed_source(
    source: str, store: CorpusStore, embedder: CorpusEmbedder
) -> EmbedReport:
    """Compute and store one source's vectors, reading only what is on disk.

    A document with neither an abstract nor a judged summary is skipped rather
    than embedded from its title alone: a vector built from six words sits near
    everything, and a layer that is confidently wrong is worth less than one
    that is honestly absent.
    """
    shard = store.load_by_name(source)
    if shard is None:
        return EmbedReport(source=source, failure="nothing ingested for it yet")

    readable = [
        (document.slug, embedded_text(document))
        for document in shard.documents
        if document.abstract or document.summary
    ]
    if not readable:
        return EmbedReport(
            source=source,
            documents=len(shard.documents),
            skipped=len(shard.documents),
            failure="no document has an abstract or a summary to embed",
        )

    try:
        vectors = await embedder.embed(tuple(text for _, text in readable))
    except Exception as error:
        logger.exception("Embedding %s failed", source)
        return EmbedReport(
            source=source,
            documents=len(shard.documents),
            failure=load_failure(error),
        )

    computed = VectorShard(
        source=source,
        embedder=embedder.identity(),
        dimensions=len(vectors[0]) if vectors else 0,
        updated_at=now_stamp(),
        vectors=tuple(
            DocumentVector(slug=slug, values=values)
            for (slug, _), values in zip(readable, vectors)
        ),
    )
    write_vectors(store, computed)
    return EmbedReport(
        source=source,
        embedder=computed.embedder,
        documents=len(shard.documents),
        embedded=len(computed.vectors),
        skipped=len(shard.documents) - len(readable),
    )


def awaiting_judgement(shard: SourceShard) -> tuple[str, ...]:
    """Documents this source holds that a vector cannot yet be computed for.

    Not the same set as the one embedding skips. A document nothing can be
    read from *and* nothing has judged is one a re-tag would rescue; one that
    has been judged and still reads empty is as good as it will get, and
    blocking on it would mean a corpus that can never finish. Only the first
    kind is named here, which is what lets a caller refuse and still converge.
    """
    return tuple(
        document.slug
        for document in shard.documents
        if not document.abstract and not document.summary and not document.tags.judged
    )


DEFAULT_EMBED_CONCURRENCY = 4
"""How many sources are embedded at once. One source is a single batched call
into whichever embedder is loaded, so this parallelises across sources and
never within one: what it is worth therefore depends on who is embedding — a
local static model is bound by the CPU it is already using, an API-backed one
by round trips it can overlap."""


async def embed_sources(
    sources: tuple[str, ...],
    store: CorpusStore,
    embedder: CorpusEmbedder,
    *,
    concurrency: int = DEFAULT_EMBED_CONCURRENCY,
) -> tuple[EmbedReport, ...]:
    """Embed several sources, one source's failure costing only itself."""
    limiter = asyncio.Semaphore(concurrency)

    async def guarded(source: str) -> EmbedReport:
        async with limiter:
            return await embed_source(source, store, embedder)

    return tuple(await asyncio.gather(*(guarded(source) for source in sources)))
