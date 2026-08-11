"""What a document is about, read off its metadata rather than asserted.

A tag is the cheap half of "who argues the other side": the question and the
document rarely share wording, but they usually share a *subject*, and a subject
is something a title and an abstract already name. So the vocabulary is a table
of rules — an id, a sentence saying what the tag covers, and the terms that
signal it — and a document's tags are whichever rules its metadata satisfies.

Derived, never stored. A tag costs one pass over a title and an abstract, so
recomputing it on every read is cheaper than keeping it in the index and having
to notice when the vocabulary changed. Adding a tag therefore reaches every
document already held, with no re-ingestion and no migration.

The surface a rule reads is metadata only — title, abstract, category — which is
the same surface the embeddings cover, and for the same reason: a PDF's text is
never extracted, so anything that needed the full text would work for half the
corpus and silently not for the other half.
"""

from abc import abstractmethod

from pydantic import BaseModel, ConfigDict, Field


def normalized_tokens(text: str) -> tuple[str, ...]:
    """The words of a text, lowercased with everything else read as a space.

    Punctuation, hyphens, and casing are noise for matching a vocabulary:
    ``Open-Weight``, ``open weights``, and ``(open weight)`` all name the same
    thing, and normalizing them here means the declared terms can be spelled the
    one plain way.
    """
    return tuple("".join(c.lower() if c.isalnum() else " " for c in text).split())


def matches_word(token: str, word: str) -> bool:
    """Whether one token is a declared word, tolerating a plural.

    Plural tolerance is the one inflection worth handling here: ``evaluation``
    and ``evaluations`` are the same subject, and the alternative is a
    vocabulary that lists both forms of every noun.
    """
    return token == word or token == f"{word}s"


def phrase_at(tokens: tuple[str, ...], start: int, phrase: tuple[str, ...]) -> bool:
    """Whether a phrase's words sit at exactly this position, in order."""
    span = len(phrase)
    if not span or start + span > len(tokens):
        return False
    return tokens[start : start + span - 1] == phrase[: span - 1] and matches_word(
        tokens[start + span - 1], phrase[span - 1]
    )


def phrase_starts(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> range:
    """The positions a phrase could start at, given how long it is."""
    return range(max(0, len(tokens) - len(phrase) + 1)) if phrase else range(0)


def contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    """Whether a phrase's words appear in order, the last one allowed a plural."""
    return any(
        phrase_at(tokens, start, phrase) for start in phrase_starts(tokens, phrase)
    )


class TagSurface(BaseModel):
    """The metadata a tag rule reads, already normalized.

    Normalized once and passed to every rule, so a document is tokenized once
    rather than once per rule in the vocabulary.
    """

    model_config = ConfigDict(frozen=True)

    tokens: tuple[str, ...] = ()
    category: str = ""

    def mentions(self, phrase: str) -> bool:
        """Whether the metadata says this phrase."""
        return contains_phrase(self.tokens, normalized_tokens(phrase))


def surface_of(
    *, title: str = "", abstract: str = "", category: str = ""
) -> TagSurface:
    """The tag surface of one document's metadata."""
    return TagSurface(
        tokens=normalized_tokens(" ".join((title, abstract, category))),
        category=category,
    )


class TagRule(BaseModel):
    """One subject a document can be about, and how it is recognised.

    A declared union rather than an injected capability: the base names the
    question and each rule answers it, so the vocabulary grows by one entry.
    Abstract without naming ``ABC``, since pydantic's metaclass is an
    ``ABCMeta`` and enforces ``applies`` already.
    """

    model_config = ConfigDict(frozen=True)

    tag_id: str = Field(description="Stable id recorded when this tag applies")
    description: str = Field(description="What the tag covers, in a sentence")

    @abstractmethod
    def applies(self, surface: TagSurface) -> bool:
        """Whether a document with this metadata carries the tag."""


class TermTag(TagRule):
    """A subject named by any of a set of terms in the title or abstract."""

    terms: tuple[str, ...] = Field(
        default=(), description="Phrases whose presence names this subject"
    )

    def applies(self, surface: TagSurface) -> bool:
        return any(surface.mentions(term) for term in self.terms)


class CategoryTag(TagRule):
    """A subject a source's own section names, whatever the wording says.

    Publishers already sort their output — a system card sits under
    ``system-cards`` — and that sorting is more reliable than any term list.
    """

    categories: tuple[str, ...] = Field(
        default=(), description="Source categories that carry this subject"
    )

    def applies(self, surface: TagSurface) -> bool:
        normalized = normalized_tokens(surface.category)
        return any(normalized == normalized_tokens(name) for name in self.categories)


DEFAULT_TAGS: tuple[TagRule, ...] = (
    TermTag(
        tag_id="evaluations",
        description="Measuring what a model can do — benchmarks, evals, elicitation",
        terms=(
            "evaluation",
            "eval",
            "benchmark",
            "capability evaluation",
            "dangerous capability",
            "elicitation",
            "task suite",
        ),
    ),
    TermTag(
        tag_id="interpretability",
        description="Reading a model's internals — features, circuits, probes",
        terms=(
            "interpretability",
            "mechanistic",
            "sparse autoencoder",
            "feature",
            "circuit",
            "probe",
            "activation",
        ),
    ),
    TermTag(
        tag_id="alignment",
        description="Making a model do what was intended — training, oversight, honesty",
        terms=(
            "alignment",
            "aligned",
            "rlhf",
            "reward model",
            "scalable oversight",
            "constitutional ai",
            "honesty",
            "sycophancy",
            "deception",
            "scheming",
        ),
    ),
    TermTag(
        tag_id="governance",
        description="Rules and institutions around AI — policy, regulation, standards",
        terms=(
            "governance",
            "policy",
            "regulation",
            "regulatory",
            "standard",
            "legislation",
            "compliance",
            "audit",
            "licensing",
            "export control",
            "international",
        ),
    ),
    TermTag(
        tag_id="compute",
        description="The hardware and scale behind training — chips, clusters, FLOP",
        terms=(
            "compute",
            "gpu",
            "chip",
            "cluster",
            "flop",
            "training run",
            "data center",
            "hardware",
            "semiconductor",
        ),
    ),
    TermTag(
        tag_id="scaling",
        description="How performance moves with scale — scaling laws, trends, projections",
        terms=(
            "scaling law",
            "scaling",
            "trend",
            "projection",
            "extrapolation",
            "growth rate",
        ),
    ),
    TermTag(
        tag_id="security",
        description="Misuse and defence — cyber, bio, model weights, safeguards",
        terms=(
            "security",
            "cyber",
            "biosecurity",
            "bioweapon",
            "misuse",
            "jailbreak",
            "safeguard",
            "weight security",
            "threat model",
            "red team",
            "attack",
        ),
    ),
    TermTag(
        tag_id="agents",
        description="Models acting over long horizons — agents, tool use, autonomy",
        terms=(
            "agent",
            "agentic",
            "autonomy",
            "autonomous",
            "tool use",
            "long horizon",
            "computer use",
        ),
    ),
    TermTag(
        tag_id="economics",
        description="What AI does to work and output — labour, productivity, adoption",
        terms=(
            "economic",
            "economy",
            "labour",
            "labor",
            "productivity",
            "adoption",
            "automation",
            "employment",
            "gdp",
            "wage",
        ),
    ),
    TermTag(
        tag_id="forecasting",
        description="Claims about when — timelines, forecasts, prediction",
        terms=(
            "forecast",
            "timeline",
            "prediction",
            "expect",
            "anticipate",
        ),
    ),
    TermTag(
        tag_id="safety-frameworks",
        description="A lab's own commitments — RSPs, preparedness, frontier frameworks",
        terms=(
            "responsible scaling",
            "preparedness framework",
            "frontier safety",
            "safety framework",
            "safety case",
            "commitment",
            "deployment policy",
        ),
    ),
    TermTag(
        tag_id="open-weights",
        description="Whether and how weights are released",
        terms=("open weight", "open source", "weight release", "release decision"),
    ),
    CategoryTag(
        tag_id="model-release",
        description="A document accompanying a released model — system and model cards",
        categories=("system-cards", "model-cards"),
    ),
    TermTag(
        tag_id="model-release",
        description="A document accompanying a released model — system and model cards",
        terms=("system card", "model card", "introducing"),
    ),
)
"""The vocabulary, as data. A caller wanting a different one passes its own
tuple — adding a tag is one entry here and needs no change to the search.

One subject may be reached by more than one rule, as ``model-release`` is by
both a category and a wording: a tag names what a document is about, not which
rule noticed."""


def unique_ids(ids: list[str]) -> tuple[str, ...]:
    """Tag ids in a stable order, each named once however many rules produced it."""
    return tuple(sorted(dict.fromkeys(ids)))


def tags_for(
    surface: TagSurface, *, rules: tuple[TagRule, ...] = DEFAULT_TAGS
) -> tuple[str, ...]:
    """Every tag a document's metadata carries."""
    return unique_ids([rule.tag_id for rule in rules if rule.applies(surface)])


def tags_of(text: str, *, rules: tuple[TagRule, ...] = DEFAULT_TAGS) -> tuple[str, ...]:
    """The tags a free-text question carries, read the same way a document's are.

    The same vocabulary on both sides is what makes the overlap mean anything:
    a question about "who is measuring dangerous capabilities" and a paper that
    never uses that phrasing still meet at ``evaluations``.
    """
    return tags_for(surface_of(title=text), rules=rules)


def declared_tag_ids(rules: tuple[TagRule, ...] = DEFAULT_TAGS) -> tuple[str, ...]:
    """Every tag id the vocabulary can produce, for a caller offering a filter."""
    return unique_ids([rule.tag_id for rule in rules])
