"""One retrieval surface: what is worded this way, and what is about this.

Two questions get asked of a corpus and they are not the same question.

*Is this claim still current?* arrives with its terms already fixed — a model
name, a threshold, a phrase someone wrote — and wants the documents that say
those words, newest first, from whichever venue is allowed to settle it. That is
lexical, and it runs over the stored Markdown, which is the reason the corpus is
plain files: a document on disk can simply be read for its wording.

*Who argues the other side?* arrives with no shared vocabulary at all. The paper
that answers it may not contain one word of the question. That is semantic, and
it runs over metadata — title, venue, section, tags, abstract — either as tag and
term overlap, which needs nothing, or as embeddings where they have been
computed.

Offering these as two tools would make the caller guess which one they needed
before knowing what is there, and guessing wrong looks exactly like an empty
corpus. So there is one call, taking both queries and the filters in any
combination, and returning one ranked list. How the two halves merge is a
declared policy rather than a weighting buried below — see ``RankingPolicy``.

**Retrieval locates; it never extracts.** A result names the stored file and
where to start in it — a page range for a PDF, a section for a Markdown
document — and the caller reads the source itself. Nothing here returns prose
lifted out of a PDF, because nothing here can: a PDF's text is never extracted
into the corpus in the first place.

**Nothing here reaches the network.** The corpus answers what is stored; the
search tools beside it find what is not stored yet. The one exception is stated
where it happens: with an embedding model explicitly configured, the semantic
half sends the *question* to that model to place it in the same space as the
stored vectors. It never fetches, and it never writes.
"""

import logging
from abc import abstractmethod
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from shlex import split as quoted_words
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.embeddings import (
    EmbeddingModel,
    EmbeddingShard,
    cosine,
    embedding_identity,
    load_embeddings,
    surface_for,
)
from inkwell.corpus.quality import MAX_HEADING_DEPTH, is_heading
from inkwell.corpus.registry import Authority, declaration_for
from inkwell.corpus.storage import (
    DATE_CHARS,
    CorpusStore,
    SourceShard,
    StoredDocument,
    now_stamp,
)
from inkwell.corpus.tags import (
    DEFAULT_TAGS,
    TagRule,
    contains_phrase,
    normalized_tokens,
    phrase_at,
    phrase_starts,
    surface_of,
    tags_for,
    tags_of,
    unique_ids,
)

logger = logging.getLogger(__name__)

PDF_READ_WINDOW = 20
"""How many pages a PDF locator asks for. The Read tool takes at most this many
in one call, so a wider range would be a request nobody can honour."""

EXCERPT_CHARS = 240
"""How much of a matching line comes back. Enough to see that the match is the
sense you meant, short of standing in for reading the document."""

STOPWORDS: tuple[str, ...] = (
    "a", "about", "after", "against", "all", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "being", "between", "both", "but",
    "by", "can", "did", "do", "does", "for", "from", "had", "has", "have", "how",
    "i", "if", "in", "into", "is", "it", "its", "more", "most", "no", "not",
    "of", "on", "one", "only", "or", "other", "our", "out", "over", "own",
    "same", "she", "should", "so", "some", "such", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "up", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with",
    "would", "you", "your",
)  # fmt: skip
"""Words that carry no subject. Dropped from a semantic question so that "who
argues the other side" is matched on ``argues`` and ``side`` rather than on
every document containing "the"."""


def content_words(text: str) -> tuple[str, ...]:
    """The words of a question that name something, each once."""
    return unique_ids(
        [word for word in normalized_tokens(text) if word not in STOPWORDS]
    )


def today_stamp() -> str:
    """Today, as the index spells a date."""
    return now_stamp()[:DATE_CHARS]


class DocumentLocator(BaseModel):
    """Where in a stored document a reader should start.

    A result carries an address rather than a passage, and the address differs
    by what the document is, so the base names the question and each kind
    answers it. Abstract without naming ``ABC``, since pydantic's metaclass is
    an ``ABCMeta`` and enforces ``instruction`` already.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    def instruction(self) -> str:
        """How to open this document at the right place, in a sentence."""


class SectionLocator(DocumentLocator):
    """A place in a stored Markdown document."""

    kind: Literal["section"] = "section"
    heading: str = Field(default="", description="Heading the match sits under")
    line: int = Field(default=0, description="1-based line of the match")

    def instruction(self) -> str:
        if not self.line:
            return "Read the file from the top; no wording was matched inside it."
        if self.heading:
            return (
                f"Read the file; the match is under '{self.heading}', "
                f"at line {self.line}."
            )
        return f"Read the file; the match is at line {self.line}."


class PageLocator(DocumentLocator):
    """A page range in a stored PDF, which is never text-extracted."""

    kind: Literal["pages"] = "pages"
    pages: str = Field(default="", description="Page range for Read's pages argument")
    page_count: int = Field(default=0, description="Pages the document has")

    def instruction(self) -> str:
        held = f" of {self.page_count}" if self.page_count else ""
        return (
            f"Read the file with pages='{self.pages}'{held}. The PDF's text is "
            "never extracted, so this is where to start rather than where the "
            "match is — widen the range if the answer is further in."
        )


type Locator = SectionLocator | PageLocator


class Candidate(BaseModel):
    """One stored document as retrieval sees it.

    The index entry, plus the standing its source declares and the tags its
    metadata carries — everything a filter or a rank asks about, and nothing
    that would need the document itself to be opened.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    venue: str = ""
    authority: Authority = "institute"
    document: StoredDocument
    path: Path
    tags: tuple[str, ...] = ()

    def key(self) -> str:
        """What identifies this document across the whole corpus."""
        return f"{self.source}/{self.document.slug}"

    def dated(self) -> str:
        return self.document.dated()

    def metadata_tokens(self) -> tuple[str, ...]:
        """The words of everything the index says about the document."""
        return normalized_tokens(
            " ".join(
                (
                    self.document.title,
                    self.document.abstract,
                    self.document.category,
                    self.venue,
                )
            )
        )


def candidates_of(
    shard: SourceShard, store: CorpusStore, *, rules: tuple[TagRule, ...] = DEFAULT_TAGS
) -> list[Candidate]:
    """Every document one source holds, tagged from its metadata."""

    def candidate(document: StoredDocument) -> Candidate:
        surface = surface_of(
            title=document.title,
            abstract=document.abstract,
            category=document.category,
        )
        return Candidate(
            source=shard.source,
            venue=shard.venue,
            authority=shard.authority,
            document=document,
            path=store.document_path(shard.source, document),
            tags=tags_for(surface, rules=rules),
        )

    return [candidate(document) for document in shard.documents]


def indexed(store: CorpusStore, source: str) -> SourceShard | None:
    """One source's index, restated against its declaration where it has one."""
    declaration = declaration_for(source)
    if declaration is None:
        return store.load_by_name(source)
    return store.load(declaration)


def corpus_candidates(
    store: CorpusStore,
    *,
    sources: tuple[str, ...] = (),
    rules: tuple[TagRule, ...] = DEFAULT_TAGS,
) -> list[Candidate]:
    """Every stored document, or every one of the named sources.

    Restricting by source here rather than after loading is what keeps a search
    over one source from reading fifteen other indexes.
    """
    wanted = sources or store.sources()
    shards = [indexed(store, source) for source in wanted]
    return [
        candidate
        for shard in shards
        if shard is not None
        for candidate in candidates_of(shard, store, rules=rules)
    ]


def admits_value(value: str, allowed: tuple[str, ...]) -> bool:
    """Whether a field passes a filter, an empty filter admitting everything."""
    return not allowed or value.casefold() in {entry.casefold() for entry in allowed}


class CorpusFilters(BaseModel):
    """Which stored documents a search may return.

    Every dimension is one a citation already has an opinion about — when it was
    published, who published it, what standing they have, and what it is about —
    so a filter here is the question a reader would ask of a reference, asked
    before reading instead of after.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[str, ...] = Field(default=(), description="Source keys to allow")
    venues: tuple[str, ...] = Field(default=(), description="Venues to allow")
    authorities: tuple[Authority, ...] = Field(
        default=(), description="Kinds of standing to allow"
    )
    categories: tuple[str, ...] = Field(
        default=(), description="Sections of a source to allow"
    )
    tags: tuple[str, ...] = Field(default=(), description="Subjects to require")
    since: str = Field(default="", description="Earliest date, as YYYY-MM-DD")
    until: str = Field(default="", description="Latest date, as YYYY-MM-DD")

    def bounded(self) -> bool:
        """Whether a date bound is in force at all."""
        return bool(self.since or self.until)

    def in_range(self, when: str) -> bool:
        """Whether a date falls inside the bounds.

        ISO order is what makes this a comparison rather than a parse, and a
        document with no date at all fails any bound: the corpus cannot claim it
        is recent, and returning it as if it were would be the wrong answer to
        exactly the question a date bound asks.
        """
        if not when:
            return not self.bounded()
        return (not self.since or when >= self.since) and (
            not self.until or when <= self.until
        )

    def admits(self, candidate: Candidate) -> bool:
        """Whether one document passes every filter in force."""
        wanted = {tag.casefold() for tag in self.tags}
        held = {tag.casefold() for tag in candidate.tags}
        return (
            admits_value(candidate.source, self.sources)
            and admits_value(candidate.venue, self.venues)
            and admits_value(candidate.authority, tuple(self.authorities))
            and admits_value(candidate.document.category, self.categories)
            and (not wanted or bool(wanted & held))
            and self.in_range(candidate.dated())
        )

    def stated(self) -> tuple[str, ...]:
        """The filters in force, named, so a result set explains its own size."""
        held = (
            ("sources", ", ".join(self.sources)),
            ("venues", ", ".join(self.venues)),
            ("authorities", ", ".join(self.authorities)),
            ("categories", ", ".join(self.categories)),
            ("tags", ", ".join(self.tags)),
            ("since", self.since),
            ("until", self.until),
        )
        return tuple(f"{name}={value}" for name, value in held if value)


class Admission(BaseModel):
    """What the filters let through, and what they cost."""

    model_config = ConfigDict(frozen=True)

    kept: list[Candidate] = Field(default_factory=list)
    undated: int = 0
    """Documents dropped for having no date while a date bound was in force.
    Reported rather than silent: a source whose pages state no date would
    otherwise look empty for every dated question, with nothing to say why."""


def admitted(candidates: list[Candidate], filters: CorpusFilters) -> Admission:
    """Apply the filters, counting what fell out for want of a date."""
    return Admission(
        kept=[candidate for candidate in candidates if filters.admits(candidate)],
        undated=sum(
            1 for candidate in candidates if filters.bounded() and not candidate.dated()
        ),
    )


class LexicalQuery(BaseModel):
    """A lexical query as the phrases it asks for.

    Quoted text stays one phrase, because ``"safety case"`` asks for the two
    words together and finding them in separate paragraphs is not an answer.
    """

    model_config = ConfigDict(frozen=True)

    phrases: tuple[tuple[str, ...], ...] = ()
    spelled: tuple[str, ...] = ()

    def asked(self) -> int:
        return len(self.phrases)

    def leads(self) -> tuple[str, ...]:
        """The first word of each phrase, for a cheap reject before tokenizing."""
        return tuple(phrase[0] for phrase in self.phrases if phrase)


def lexical_query(text: str) -> LexicalQuery:
    """Read a lexical query, honouring quotes."""
    if not text.strip():
        return LexicalQuery()
    try:
        parts = quoted_words(text)
    except ValueError:
        logger.debug("Unbalanced quoting in %r; reading it as plain words", text)
        parts = list(normalized_tokens(text))
    read = [(part, normalized_tokens(part)) for part in parts]
    kept = [(part, phrase) for part, phrase in read if phrase]
    return LexicalQuery(
        phrases=tuple(phrase for _, phrase in kept),
        spelled=tuple(part for part, _ in kept),
    )


class LexicalMatch(BaseModel):
    """Where a lexical query was found in one document."""

    model_config = ConfigDict(frozen=True)

    matched: tuple[str, ...] = Field(
        default=(), description="Query phrases this document actually contains"
    )
    asked: int = Field(default=0, description="Phrases the query asked for")
    occurrences: int = Field(default=0, description="How often they appear")
    excerpt: str = Field(
        default="", description="The matching line, whitespace collapsed"
    )
    heading: str = Field(default="", description="Heading the match sits under")
    line: int = Field(default=0, description="1-based line of the first match")
    in_text: bool = Field(
        default=True,
        description="False when only the title matched — a PDF stores no text",
    )

    def coverage(self) -> float:
        """The share of the asked-for phrases this document has.

        Coverage rather than count is what ranks: a document using every term
        once is answering the question, and one repeating a single term forty
        times is usually a glossary.
        """
        return len(self.matched) / self.asked if self.asked else 0.0


class LexicalScan(BaseModel):
    """How much of the corpus one lexical pass may open.

    Declared so a corpus that outgrows what is comfortable to read in a tool
    call degrades into "the first N documents, and it says so" rather than into
    a call that never returns.
    """

    model_config = ConfigDict(frozen=True)

    max_documents: int = 2000
    max_bytes: int = 2_000_000


def collapsed(line: str) -> str:
    """One line as a single-spaced excerpt, cut to a readable length."""
    joined = " ".join(line.split())
    return joined if len(joined) <= EXCERPT_CHARS else f"{joined[:EXCERPT_CHARS]}…"


def heading_text(line: str) -> str:
    """The text of a Markdown heading, or empty for a line that is not one."""
    if not is_heading(line):
        return ""
    depth = next(n for n in range(MAX_HEADING_DEPTH, 0, -1) if line.startswith("#" * n))
    return line[depth:].strip()


class LineHit(BaseModel):
    """One line of a document carrying a query phrase, and what it sits under."""

    model_config = ConfigDict(frozen=True)

    line: int = 0
    heading: str = ""
    excerpt: str = ""


def first_hit(text: str, query: LexicalQuery) -> LineHit:
    """Where in a Markdown document the query first appears, and under what.

    Walks the document once, remembering the last heading seen, so the section a
    match sits under is a fact about the file rather than a guess from the prose
    around it.
    """

    def hits() -> Iterator[LineHit]:
        heading = ""
        for number, line in enumerate(text.splitlines(), start=1):
            heading = heading_text(line) or heading
            tokens = normalized_tokens(line)
            if any(contains_phrase(tokens, phrase) for phrase in query.phrases):
                yield LineHit(line=number, heading=heading, excerpt=collapsed(line))

    return next(hits(), LineHit())


def readable_text(path: Path, scan: LexicalScan) -> str:
    """A stored document's Markdown, or empty where it cannot be read.

    A document too large to scan is skipped rather than truncated: half a
    document answers "the term is not in here" wrongly, and a corpus that lies
    about an absence is worse than one that admits a gap.
    """
    try:
        if path.stat().st_size > scan.max_bytes:
            logger.info("Skipping %s in the lexical scan: larger than the limit", path)
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("Could not read %s during a corpus search", path, exc_info=True)
        return ""


def phrase_hits(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> int:
    """How often a phrase appears in a token stream."""
    return sum(
        1 for start in phrase_starts(tokens, phrase) if phrase_at(tokens, start, phrase)
    )


def lexical_match(
    candidate: Candidate, query: LexicalQuery, scan: LexicalScan
) -> LexicalMatch | None:
    """Match one document's stored text and title against a lexical query.

    The title is matched separately from the text, and the result says which it
    was: a PDF stores no text at all, so title-only is the most the lexical half
    can ever say about one, and implying otherwise would send a reader looking
    for words that were never in the corpus.
    """
    if not query.phrases:
        return None
    text = "" if candidate.document.is_pdf() else readable_text(candidate.path, scan)
    lowered = text.casefold()
    title_tokens = normalized_tokens(candidate.document.title)
    likely = any(lead in lowered for lead in query.leads())
    tokens = normalized_tokens(text) if likely else ()

    counted = [
        (spelled, phrase_hits(tokens, phrase), phrase_hits(title_tokens, phrase))
        for spelled, phrase in zip(query.spelled, query.phrases)
    ]
    matched = tuple(spelled for spelled, inside, titled in counted if inside or titled)
    if not matched:
        return None

    in_text = any(inside for _, inside, _ in counted)
    hit = (
        first_hit(text, query)
        if in_text
        else LineHit(excerpt=collapsed(candidate.document.title))
    )
    return LexicalMatch(
        matched=matched,
        asked=query.asked(),
        occurrences=sum(inside + titled for _, inside, titled in counted),
        excerpt=hit.excerpt,
        heading=hit.heading,
        line=hit.line,
        in_text=in_text,
    )


class LexicalPass(BaseModel):
    """What the lexical half found, and how much of the corpus it opened."""

    model_config = ConfigDict(frozen=True)

    matches: dict[str, LexicalMatch] = Field(default_factory=dict)
    scanned: int = 0
    truncated: bool = False

    def matched(self, key: str) -> LexicalMatch | None:
        return self.matches[key] if key in self.matches else None


def lexical_pass(
    candidates: list[Candidate], query: LexicalQuery, scan: LexicalScan
) -> LexicalPass:
    """Run the lexical half over the stored Markdown, up to the scan limit."""
    if not query.phrases:
        return LexicalPass()
    opened = candidates[: scan.max_documents]
    found = [(candidate, lexical_match(candidate, query, scan)) for candidate in opened]
    return LexicalPass(
        matches={
            candidate.key(): match for candidate, match in found if match is not None
        },
        scanned=len(opened),
        truncated=len(candidates) > len(opened),
    )


class SemanticMatch(BaseModel):
    """How close one document is to a question that may share none of its words."""

    model_config = ConfigDict(frozen=True)

    score: float = Field(default=0.0, description="Closeness, 0 to 1")
    shared_tags: tuple[str, ...] = Field(
        default=(), description="Subjects the question and the document share"
    )
    shared_terms: tuple[str, ...] = Field(
        default=(), description="Words of the question the metadata also uses"
    )


class SemanticPass(BaseModel):
    """What the semantic half made of a question, and what it could not place."""

    model_config = ConfigDict(frozen=True)

    matches: dict[str, SemanticMatch] = Field(default_factory=dict)
    unplaced: int = Field(
        default=0, description="Documents with no vector, so nothing to compare"
    )
    basis: str = Field(default="", description="What the scores are, in a phrase")

    def placed(self, key: str) -> SemanticMatch | None:
        return self.matches[key] if key in self.matches else None


class SemanticSimilarity(BaseModel):
    """Ranks stored documents against a question by subject rather than wording.

    A seam, only ever injected: ``CorpusSearch`` holds whichever half this
    corpus can actually run — overlap over tags and metadata, which needs
    nothing, or embeddings where they have been computed — and every caller
    above holds the search, never this.
    """

    model_config = ConfigDict(frozen=True)

    @abstractmethod
    async def rank(self, question: str, candidates: list[Candidate]) -> SemanticPass:
        """Score the candidates this question reaches, keyed by candidate key."""


class MetadataOverlap(SemanticSimilarity):
    """Subject overlap: the tags a question carries against the tags a document
    carries, plus whichever of its words the metadata also uses.

    The default half, and what makes a corpus searchable with no key, no
    network, and nothing precomputed. Coarser than embeddings and honest about
    it — a tag is a subject, so this finds the right shelf rather than the right
    sentence, which is what a hand-off to Read wanted anyway.
    """

    rules: tuple[TagRule, ...] = DEFAULT_TAGS
    tag_weight: float = 0.7
    term_weight: float = 0.3

    async def rank(self, question: str, candidates: list[Candidate]) -> SemanticPass:
        asked_tags = tags_of(question, rules=self.rules)
        asked_terms = content_words(question)
        basis = "tag and metadata overlap (no embeddings configured)"
        if not asked_tags and not asked_terms:
            return SemanticPass(basis=basis)
        scored = [
            (candidate, self.compare(candidate, asked_tags, asked_terms))
            for candidate in candidates
        ]
        return SemanticPass(
            matches={
                candidate.key(): match for candidate, match in scored if match.score > 0
            },
            basis=basis,
        )

    def compare(
        self,
        candidate: Candidate,
        asked_tags: tuple[str, ...],
        asked_terms: tuple[str, ...],
    ) -> SemanticMatch:
        """One document against one question, on subjects and then on words."""
        shared_tags = tuple(tag for tag in asked_tags if tag in candidate.tags)
        held_terms = {token for token in candidate.metadata_tokens()}
        shared_terms = tuple(term for term in asked_terms if term in held_terms)
        tag_share = len(shared_tags) / len(asked_tags) if asked_tags else 0.0
        term_share = len(shared_terms) / len(asked_terms) if asked_terms else 0.0
        return SemanticMatch(
            score=round(self.tag_weight * tag_share + self.term_weight * term_share, 4),
            shared_tags=shared_tags,
            shared_terms=shared_terms,
        )


class EmbeddedSimilarity(SemanticSimilarity):
    """Cosine against the vectors stored beside each source's index.

    The question is embedded by the same model that embedded the metadata — the
    one network call retrieval ever makes, and only where a model was configured
    on purpose. A document whose stored vector is missing or stale is counted as
    unplaced rather than scored as unrelated, because those are different facts.
    """

    model: EmbeddingModel
    shards: dict[str, EmbeddingShard] = Field(default_factory=dict)
    rules: tuple[TagRule, ...] = DEFAULT_TAGS

    def vector_for(self, candidate: Candidate) -> tuple[float, ...]:
        """The stored vector for this exact document, empty when it is stale."""
        if candidate.source not in self.shards:
            return ()
        surface = surface_for(
            candidate.document, venue=candidate.venue, rules=self.rules
        )
        identity = embedding_identity(candidate.document.content_sha256, surface)
        return self.shards[candidate.source].vector_for(
            candidate.document.slug, identity
        )

    async def rank(self, question: str, candidates: list[Candidate]) -> SemanticPass:
        basis = f"cosine over metadata embeddings ({self.model.identifier})"
        asked = await self.model.embed_one(question)
        if not asked:
            logger.warning(
                "%s returned no vector for the question", self.model.identifier
            )
            return SemanticPass(basis=basis)
        held = [(candidate, self.vector_for(candidate)) for candidate in candidates]
        placed = [
            (candidate, max(0.0, cosine(asked, vector)))
            for candidate, vector in held
            if vector
        ]
        asked_tags = tags_of(question, rules=self.rules)
        return SemanticPass(
            matches={
                candidate.key(): SemanticMatch(
                    score=round(score, 4),
                    shared_tags=tuple(
                        tag for tag in asked_tags if tag in candidate.tags
                    ),
                )
                for candidate, score in placed
                if score > 0
            },
            unplaced=len(held) - len(placed),
            basis=basis,
        )


class ScoredCandidate(BaseModel):
    """One document with whatever each half made of it, before ranking."""

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    lexical: LexicalMatch | None = None
    semantic: SemanticMatch | None = None
    score: float = 0.0

    def lexical_score(self) -> float:
        return self.lexical.coverage() if self.lexical is not None else 0.0

    def semantic_score(self) -> float:
        return self.semantic.score if self.semantic is not None else 0.0

    def found_by_both(self) -> bool:
        return bool(self.lexical_score()) and bool(self.semantic_score())

    def reached(self) -> bool:
        """Whether either half found this document at all."""
        return self.lexical is not None or self.semantic is not None


def days_between(earlier: str, later: str) -> int:
    """Days from one ISO date to another, or -1 where either is unreadable."""
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return -1


AUTHORITY_STANDING: dict[Authority, float] = {
    "first-party": 1.0,
    "government": 0.9,
    "institute": 0.8,
    "independent": 0.6,
}
"""What a source's kind is worth before any weighting. First-party leads because
most of what this corpus is asked is what a lab did, which is the one thing that
lab is the authority on — and the weight on it stays small precisely because it
is the wrong order for how much that lab's word is worth about anyone else."""


class RankingPolicy(BaseModel):
    """How the two halves become one order.

    Every number that decides a rank is a field, so changing the balance means
    constructing a different policy — ``RankingPolicy(semantic_weight=2.0)`` —
    rather than editing the merge. The defaults say: both halves count equally,
    a document found by *both* is the strongest signal there is, and recency,
    quality, and standing break ties without overturning relevance.

    Each half is scaled to its own best result before weighting, because a
    coverage fraction and a cosine are not the same kind of number, and adding
    them raw would let whichever half happens to run hot decide every order.
    """

    model_config = ConfigDict(frozen=True)

    lexical_weight: float = Field(
        default=1.0, description="Weight on the lexical half's relative score"
    )
    semantic_weight: float = Field(
        default=1.0, description="Weight on the semantic half's relative score"
    )
    both_halves_bonus: float = Field(
        default=0.25, description="Lift for a document both halves found"
    )
    recency_weight: float = Field(default=0.15, description="Weight on freshness")
    recency_window_days: int = Field(
        default=1095, description="Age at which a document counts as not recent"
    )
    quality_weight: float = Field(
        default=0.1, description="Weight on the capture's quality score"
    )
    authority_weight: float = Field(
        default=0.1, description="Weight on the source's standing"
    )
    standing: dict[Authority, float] = Field(
        default_factory=lambda: dict(AUTHORITY_STANDING),
        description="What each kind of source's standing is worth",
    )
    limit: int = Field(default=20, description="How many results come back")

    def recency(self, when: str, *, today: str) -> float:
        """One for a document published today, zero at the window's edge."""
        age = days_between(when, today) if when else -1
        if age < 0:
            return 0.0
        return max(0.0, 1.0 - age / self.recency_window_days)

    def standing_of(self, authority: Authority) -> float:
        return self.standing[authority] if authority in self.standing else 0.5

    def score(
        self, scored: ScoredCandidate, *, lexical: float, semantic: float, today: str
    ) -> float:
        """One document's rank, from the two halves and the tie-breakers."""
        document = scored.candidate.document
        return round(
            self.lexical_weight * lexical
            + self.semantic_weight * semantic
            + (self.both_halves_bonus if scored.found_by_both() else 0.0)
            + self.recency_weight * self.recency(scored.candidate.dated(), today=today)
            + self.quality_weight * document.quality.score
            + self.authority_weight * self.standing_of(scored.candidate.authority),
            4,
        )

    def merged(
        self, scored: list[ScoredCandidate], *, today: str
    ) -> list[ScoredCandidate]:
        """Rank every document, best first, and keep the top of the list."""
        best_lexical = max((one.lexical_score() for one in scored), default=0.0)
        best_semantic = max((one.semantic_score() for one in scored), default=0.0)

        def relative(value: float, best: float) -> float:
            return value / best if best else 0.0

        ranked = [
            one.model_copy(
                update={
                    "score": self.score(
                        one,
                        lexical=relative(one.lexical_score(), best_lexical),
                        semantic=relative(one.semantic_score(), best_semantic),
                        today=today,
                    )
                }
            )
            for one in scored
        ]
        ordered = sorted(ranked, key=lambda one: (-one.score, one.candidate.key()))
        return ordered[: self.limit]

    def explain(self) -> str:
        """The policy in a sentence, so a result set says how it was ordered."""
        return (
            f"lexical×{self.lexical_weight} + semantic×{self.semantic_weight} "
            f"(+{self.both_halves_bonus} when both halves found it), then "
            f"recency×{self.recency_weight} over {self.recency_window_days}d, "
            f"quality×{self.quality_weight}, standing×{self.authority_weight}"
        )


DEFAULT_RANKING = RankingPolicy()
"""The merge every caller gets unless it declares another one."""


def locator_for(candidate: Candidate, match: LexicalMatch | None) -> Locator:
    """Where the caller should start reading this result.

    A PDF gets a page range and nothing more, because nothing more is known: its
    text was never extracted, so the corpus can say the document is relevant and
    where it begins, and the reading settles the rest.
    """
    document = candidate.document
    if document.is_pdf():
        last = min(document.page_count, PDF_READ_WINDOW) or PDF_READ_WINDOW
        return PageLocator(pages=f"1-{last}", page_count=document.page_count)
    if match is None or not match.in_text:
        return SectionLocator()
    return SectionLocator(heading=match.heading, line=match.line)


class SearchResult(BaseModel):
    """One document worth reading, and everything about it but the document."""

    source: str = Field(description="Corpus source key")
    slug: str = Field(description="Identifier within the source")
    title: str = Field(description="Document title")
    venue: str = Field(description="Venue a citation of it should name")
    authority: str = Field(description="What standing its word has")
    category: str = Field(description="Section of the source it came from")
    tags: tuple[str, ...] = Field(default=(), description="Subjects it covers")
    url: str = Field(default="", description="Where it was published")
    path: str = Field(description="Stored file to Read")
    kind: str = Field(description="'markdown' or 'pdf'")
    date: str = Field(default="", description="Publication date, or the fetch date")
    published: str = Field(
        default="", description="Publication date where the page stated one"
    )
    locator: Locator = Field(description="Where in the document to start reading")
    read_next: str = Field(description="How to open it, in a sentence")
    lexical: LexicalMatch | None = Field(
        default=None, description="What the lexical half found, where it ran"
    )
    semantic: SemanticMatch | None = Field(
        default=None, description="What the semantic half found, where it ran"
    )
    score: float = Field(default=0.0, description="Rank under the declared policy")
    abstract: str = Field(default="", description="Opening prose, for relevance")
    words: int = Field(default=0, description="Word count (0 for a PDF)")
    page_count: int = Field(default=0, description="Pages, for a PDF")
    quality_score: float = Field(default=1.0, description="Heuristic score, 0-1")
    quality_flags: tuple[str, ...] = Field(
        default=(), description="Quality rule ids that fired"
    )


def result_of(scored: ScoredCandidate) -> SearchResult:
    """One ranked candidate as the caller receives it."""
    candidate = scored.candidate
    document = candidate.document
    locator = locator_for(candidate, scored.lexical)
    return SearchResult(
        source=candidate.source,
        slug=document.slug,
        title=document.title,
        venue=candidate.venue,
        authority=candidate.authority,
        category=document.category,
        tags=candidate.tags,
        url=document.url,
        path=str(candidate.path),
        kind=document.kind,
        date=candidate.dated(),
        published=document.published,
        locator=locator,
        read_next=locator.instruction(),
        lexical=scored.lexical,
        semantic=scored.semantic,
        score=scored.score,
        abstract=document.abstract,
        words=document.words,
        page_count=document.page_count,
        quality_score=document.quality.score,
        quality_flags=document.quality.fired,
    )


class SearchRequest(BaseModel):
    """One question asked of the corpus, in both of the ways it can be asked."""

    model_config = ConfigDict(frozen=True)

    lexical: str = ""
    semantic: str = ""
    filters: CorpusFilters = CorpusFilters()
    limit: int = 0

    def asks_nothing(self) -> bool:
        """Whether this asks nothing at all — no query and nothing to browse by."""
        return not (
            self.lexical.strip() or self.semantic.strip() or self.filters.stated()
        )


class SearchOutcome(BaseModel):
    """The ranked results, and what a caller needs in order to trust an absence."""

    results: list[SearchResult] = Field(default_factory=list)
    held: int = Field(default=0, description="Documents the searched sources hold")
    admitted: int = Field(default=0, description="Documents the filters allowed")
    scanned: int = Field(default=0, description="Stored documents opened for wording")
    truncated: bool = Field(
        default=False, description="True when the scan limit stopped it short"
    )
    undated: int = Field(
        default=0, description="Documents dropped for having no date under a date bound"
    )
    unplaced: int = Field(
        default=0, description="Documents the semantic half had no vector for"
    )
    basis: str = Field(default="", description="What the semantic scores are")
    ranked_by: str = Field(default="", description="The ranking policy, in a sentence")
    filters: tuple[str, ...] = Field(default=(), description="Filters in force")


class CorpusSearch(BaseModel):
    """Retrieval over one corpus: two halves, merged by a declared policy.

    A plain class composing the seam rather than a capability itself — callers
    hold this, never a similarity — so which semantic half runs is a
    construction detail and never something a caller has to know.
    """

    model_config = ConfigDict(frozen=True)

    store: CorpusStore
    similarity: SemanticSimilarity
    ranking: RankingPolicy = DEFAULT_RANKING
    scan: LexicalScan = LexicalScan()
    rules: tuple[TagRule, ...] = DEFAULT_TAGS

    def policy(self, limit: int) -> RankingPolicy:
        """The ranking for this call: the declared one, or its limit overridden."""
        if limit <= 0:
            return self.ranking
        return self.ranking.model_copy(update={"limit": limit})

    async def run(self, request: SearchRequest, *, today: str = "") -> SearchOutcome:
        """Answer one request, running whichever halves it actually asked for."""
        candidates = corpus_candidates(
            self.store, sources=request.filters.sources, rules=self.rules
        )
        allowed = admitted(candidates, request.filters)
        query = lexical_query(request.lexical)
        found = lexical_pass(allowed.kept, query, self.scan)
        semantic = (
            await self.similarity.rank(request.semantic, allowed.kept)
            if request.semantic.strip()
            else SemanticPass()
        )
        scored = [
            ScoredCandidate(
                candidate=candidate,
                lexical=found.matched(candidate.key()),
                semantic=semantic.placed(candidate.key()),
            )
            for candidate in allowed.kept
        ]
        asked = bool(query.phrases or request.semantic.strip())
        reached = [one for one in scored if one.reached() or not asked]
        policy = self.policy(request.limit)
        return SearchOutcome(
            results=[
                result_of(one)
                for one in policy.merged(reached, today=today or today_stamp())
            ],
            held=len(candidates),
            admitted=len(allowed.kept),
            scanned=found.scanned,
            truncated=found.truncated,
            undated=allowed.undated,
            unplaced=semantic.unplaced,
            basis=semantic.basis,
            ranked_by=policy.explain(),
            filters=request.filters.stated(),
        )


def similarity_for(
    store: CorpusStore,
    sources: tuple[str, ...],
    *,
    model: EmbeddingModel | None,
    rules: tuple[TagRule, ...] = DEFAULT_TAGS,
) -> SemanticSimilarity:
    """The semantic half this corpus can actually run right now.

    Embeddings only where a model is configured *and* vectors have been
    computed; otherwise tag and metadata overlap, which is always available.
    Falling back rather than failing is the point: a corpus with no vectors is
    still searchable, so turning embeddings on is an improvement rather than a
    prerequisite.
    """
    if model is None:
        return MetadataOverlap(rules=rules)
    known = sources or store.sources()
    shards = {source: load_embeddings(store, source) for source in known}
    if not any(shard.documents for shard in shards.values()):
        logger.info(
            "%s is configured but no vectors are stored; ranking semantically by "
            "tags and metadata until `lup-devtools corpus embed` has run",
            model.identifier,
        )
        return MetadataOverlap(rules=rules)
    return EmbeddedSimilarity(model=model, shards=shards, rules=rules)
