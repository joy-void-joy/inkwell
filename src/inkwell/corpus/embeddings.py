"""Vectors over what the index already says — an addition, never a dependency.

Retrieval works with no embeddings at all: the semantic half then ranks by the
tag vocabulary and the words of a document's metadata, which needs no key, no
network, and no recompute. Embeddings are what you turn on when that is not
discriminating enough, and everything here is shaped by that being optional —
nothing else in the corpus fails, or changes behaviour, when no model is
configured.

**Metadata and abstracts, never full text.** What gets embedded is the title,
venue, category, tags, and the abstract the index already keeps. Embedding full
text would mean extracting a PDF's text, which this corpus refuses, so the
vectors would cover the Markdown half of the corpus and quietly not the other
half — and a search that silently sees half the material is worse than one that
sees all of it a little less sharply.

**Identity, not time, decides what is recomputed.** Each vector is stored beside
its source's index under the digest of the content hash and the exact text that
was embedded, so a run over an unchanged corpus embeds nothing and costs nothing.
Changing the model or the tag vocabulary changes that digest, which is what makes
either safe to change.
"""

import logging
from abc import abstractmethod
from math import sqrt
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inkwell.corpus.storage import (
    CorpusStore,
    SourceShard,
    StoredDocument,
    content_digest,
    now_stamp,
)
from inkwell.corpus.tags import DEFAULT_TAGS, TagRule, surface_of, tags_for

logger = logging.getLogger(__name__)

type Vector = tuple[float, ...]

DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
"""Where an unconfigured endpoint points. The wire format below is the one
every embedding vendor and local server speaks, so pointing this at another
provider — or at a model running on the machine — is a setting rather than a
second implementation."""

DEFAULT_EMBEDDING_BATCH = 64
DEFAULT_EMBEDDING_TIMEOUT = 60.0


def cosine(left: Vector, right: Vector) -> float:
    """How close two vectors point, as a number between -1 and 1.

    Zero for vectors of different lengths or for a zero vector, which is what a
    model returns when it was given nothing worth embedding — treating that as
    "no similarity" keeps a degenerate entry from outranking real matches.
    """
    if len(left) != len(right) or not left:
        return 0.0
    scale = sqrt(sum(value * value for value in left)) * sqrt(
        sum(value * value for value in right)
    )
    if not scale:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / scale


def metadata_surface(
    *,
    title: str = "",
    venue: str = "",
    category: str = "",
    tags: tuple[str, ...] = (),
    abstract: str = "",
) -> str:
    """The text that stands in for a document, and the whole of what is embedded.

    Written out as a labelled line per field rather than as bare concatenation,
    because the label is context a model reads: ``venue: METR`` places the
    document, where a loose ``METR`` is just a token.
    """
    stated = (
        ("title", title),
        ("venue", venue),
        ("section", category),
        ("tags", ", ".join(tags)),
        ("abstract", abstract),
    )
    return "\n".join(f"{name}: {value}" for name, value in stated if value)


def surface_for(
    document: StoredDocument,
    *,
    venue: str = "",
    rules: tuple[TagRule, ...] = DEFAULT_TAGS,
) -> str:
    """The embedded surface of one stored document, tags included."""
    surface = surface_of(
        title=document.title, abstract=document.abstract, category=document.category
    )
    return metadata_surface(
        title=document.title,
        venue=venue,
        category=document.category,
        tags=tags_for(surface, rules=rules),
        abstract=document.abstract,
    )


def embedding_identity(content_sha256: str, surface: str) -> str:
    """What a stored vector is a vector *of*.

    Both halves matter: the content hash catches a document that was rewritten,
    and the surface catches a vocabulary or a formatting change that would make
    the stored vector an answer to a question nobody asked any more.
    """
    return content_digest(f"{content_sha256}\n{surface}".encode())


class EmbeddingModel(BaseModel):
    """Turns text into vectors.

    A seam, only ever injected: retrieval and the refresh below hold whichever
    implementation was configured, and a test holds one that returns fixed
    vectors without a network. ``identifier`` is recorded beside the vectors, so
    changing the model invalidates them rather than silently mixing two spaces.
    """

    model_config = ConfigDict(frozen=True)

    identifier: str = Field(description="Which model produced a vector")

    async def embed_one(self, text: str) -> Vector:
        """One text's vector, for the query side of a search."""
        vectors = await self.embed((text,))
        return vectors[0] if vectors else ()

    @abstractmethod
    async def embed(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        """One vector per text, in the order the texts were given."""


class EmbeddingDatum(BaseModel):
    """One vector as an embeddings endpoint sends it."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    embedding: Vector = ()


class EmbeddingResponse(BaseModel):
    """What an embeddings endpoint answers with."""

    model_config = ConfigDict(extra="ignore")

    data: list[EmbeddingDatum] = Field(default_factory=list)

    def vectors(self) -> tuple[Vector, ...]:
        """The vectors in the order they were asked for, not the order they came."""
        ordered = sorted(self.data, key=lambda datum: datum.index)
        return tuple(item.embedding for item in ordered)


class EmbeddingFailed(Exception):
    """An embedding call that did not come back with vectors."""


class HttpEmbeddingModel(EmbeddingModel):
    """An embeddings endpoint speaking the ``POST /embeddings`` wire format.

    One implementation covers every vendor that speaks it and every local server
    that imitates it, so which model runs — and whether it runs on this machine
    at all — is configuration rather than code.
    """

    api_key: str = ""
    base_url: str = DEFAULT_EMBEDDING_BASE_URL
    batch: int = DEFAULT_EMBEDDING_BATCH
    timeout: float = DEFAULT_EMBEDDING_TIMEOUT

    def headers(self) -> httpx.Headers:
        """What every request carries, with a key only where one is configured."""
        bearer = f"Bearer {self.api_key}" if self.api_key else ""
        stated = (("content-type", "application/json"), ("authorization", bearer))
        return httpx.Headers([(name, value) for name, value in stated if value])

    def endpoint(self) -> str:
        """The embeddings URL under the configured base, read as a URL."""
        parsed = urlsplit(self.base_url)
        path = PurePosixPath(parsed.path or "/") / "embeddings"
        return urlunsplit((parsed.scheme, parsed.netloc, str(path), "", ""))

    async def embed(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        if not texts:
            return ()
        batches = [
            texts[start : start + self.batch]
            for start in range(0, len(texts), self.batch)
        ]
        async with httpx.AsyncClient(
            timeout=self.timeout, headers=self.headers()
        ) as client:
            per_batch = [await self.embed_batch(client, batch) for batch in batches]
        return tuple(vector for batch in per_batch for vector in batch)

    async def embed_batch(
        self, client: httpx.AsyncClient, texts: tuple[str, ...]
    ) -> tuple[Vector, ...]:
        """One request's worth of vectors, or a failure naming what went wrong."""
        try:
            response = await client.post(
                self.endpoint(),
                json={"model": self.identifier, "input": list(texts)},
            )
            response.raise_for_status()
            parsed = EmbeddingResponse.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise EmbeddingFailed(
                f"{self.identifier} at {self.base_url} did not return vectors: {error}"
            ) from error
        vectors = parsed.vectors()
        if len(vectors) != len(texts):
            raise EmbeddingFailed(
                f"{self.identifier} returned {len(vectors)} vectors for "
                f"{len(texts)} texts"
            )
        return vectors


class EmbeddedDocument(BaseModel):
    """One document's vector, and what it is a vector of."""

    model_config = ConfigDict(frozen=True)

    slug: str
    identity: str
    vector: Vector = ()


class EmbeddingShard(BaseModel):
    """One source's vectors, beside that source's index.

    Sharded per source for the same reason the index is: a top-up touches one
    source, and re-embedding it never rewrites another's file.
    """

    model_config = ConfigDict(validate_assignment=True)

    source: str
    model: str = ""
    updated_at: str = ""
    documents: list[EmbeddedDocument] = Field(default_factory=list)

    def by_slug(self) -> dict[str, EmbeddedDocument]:
        return {entry.slug: entry for entry in self.documents}

    def vector_for(self, slug: str, identity: str) -> Vector:
        """The stored vector for this exact identity, empty when there is none."""
        held = self.by_slug()
        if slug not in held or held[slug].identity != identity:
            return ()
        return held[slug].vector


def load_embeddings(store: CorpusStore, source: str) -> EmbeddingShard:
    """A source's stored vectors, or an empty shard where none were computed.

    An unreadable file is reported and treated as absent: vectors are derived
    data, so the recovery is to compute them again rather than to stop.
    """
    path = store.embeddings_path(source)
    if not path.is_file():
        return EmbeddingShard(source=source)
    try:
        return EmbeddingShard.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        logger.warning("Unreadable embeddings at %s", path, exc_info=True)
        return EmbeddingShard(source=source)


def save_embeddings(store: CorpusStore, shard: EmbeddingShard) -> None:
    """Write a source's vectors beside its index."""
    path = store.embeddings_path(shard.source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shard.model_dump_json() + "\n", encoding="utf-8")


class EmbeddingRefresh(BaseModel):
    """What one refresh did, in the terms that say whether it cost anything."""

    model_config = ConfigDict(frozen=True)

    source: str
    model: str = ""
    embedded: int = 0
    unchanged: int = 0
    dropped: int = 0

    def summary(self) -> str:
        return (
            f"{self.source}: embedded {self.embedded} | unchanged {self.unchanged} "
            f"| dropped {self.dropped}"
        )


class PlannedEmbedding(BaseModel):
    """One document a refresh looked at, and whether it still has its vector.

    Planning before calling is what keeps the skip honest: every document is
    resolved to the text that would be embedded and the identity that text has,
    and only the ones whose stored vector does not match that identity are sent.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    identity: str
    surface: str
    vector: Vector = ()

    def kept(self) -> EmbeddedDocument:
        return EmbeddedDocument(
            slug=self.slug, identity=self.identity, vector=self.vector
        )


def plan_embeddings(
    shard: SourceShard,
    stored: EmbeddingShard,
    *,
    reusable: bool,
    rules: tuple[TagRule, ...] = DEFAULT_TAGS,
) -> list[PlannedEmbedding]:
    """What every stored document would be embedded as, and what is already held."""

    def planned(document: StoredDocument) -> PlannedEmbedding:
        surface = surface_for(document, venue=shard.venue, rules=rules)
        identity = embedding_identity(document.content_sha256, surface)
        return PlannedEmbedding(
            slug=document.slug,
            identity=identity,
            surface=surface,
            vector=stored.vector_for(document.slug, identity) if reusable else (),
        )

    return [planned(document) for document in shard.documents]


async def refresh_embeddings(
    store: CorpusStore,
    shard: SourceShard,
    model: EmbeddingModel,
    *,
    rules: tuple[TagRule, ...] = DEFAULT_TAGS,
) -> EmbeddingRefresh:
    """Bring one source's vectors up to date, embedding only what changed.

    A source whose documents and vocabulary are untouched calls the model zero
    times — the point of keying on identity rather than on a timestamp.
    """
    stored = load_embeddings(store, shard.source)
    plan = plan_embeddings(
        shard, stored, reusable=stored.model == model.identifier, rules=rules
    )
    kept = [entry.kept() for entry in plan if entry.vector]
    pending = [entry for entry in plan if not entry.vector]

    vectors = await model.embed(tuple(entry.surface for entry in pending))
    embedded = [
        EmbeddedDocument(slug=entry.slug, identity=entry.identity, vector=vector)
        for entry, vector in zip(pending, vectors)
    ]
    save_embeddings(
        store,
        EmbeddingShard(
            source=shard.source,
            model=model.identifier,
            updated_at=now_stamp(),
            documents=sorted([*kept, *embedded], key=lambda entry: entry.slug),
        ),
    )
    return EmbeddingRefresh(
        source=shard.source,
        model=model.identifier,
        embedded=len(embedded),
        unchanged=len(kept),
        dropped=max(0, len(stored.documents) - len(kept)),
    )


def configured_embedding_model() -> EmbeddingModel | None:
    """The embedding model this environment configured, or None for none.

    None is the default and the ordinary case: the semantic half then ranks by
    tags and metadata wording, and the corpus stays searchable with nothing
    configured at all.
    """
    from inkwell.agent.config import current_settings

    settings = current_settings()
    if not settings.corpus_embedding_model:
        return None
    return HttpEmbeddingModel(
        identifier=settings.corpus_embedding_model,
        api_key=settings.corpus_embedding_api_key or "",
        base_url=settings.corpus_embedding_base_url,
    )
