"""The tag vocabulary, and the tags one document carries.

At this corpus's scale — hundreds to low thousands of documents — retrieval is
a browse rather than a query: an agent reads what is there and narrows. Titles
alone do not narrow, which is what tags are for, and this module is where the
tags a browse may offer are declared.

The vocabulary is a **declared core, plus whatever a judgement proposes beyond
it**. A closed set misses every subject nobody thought to declare; a fully open
one drifts into near-duplicates and singletons until browsing it is worse than
scanning. So a document's tags are kept split by where they came from: what the
declared vocabulary holds goes to ``core``, which is what a browse filters on,
and anything else goes to ``free``, which is a review queue — a free tag that
keeps recurring is one line of data away from being core.

Adding a tag is a change to the declaration below, never a string an ingestion
run invents. Nothing here reaches the registry or the store, which is what lets
the registry declare a source's tags with these very terms instead of with
strings that could drift from them; assignment itself lives in ``tagging``.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

type TagFacet = Literal["subject", "method", "organization"]
"""What a tag says about a document.

- ``subject`` — what it is about.
- ``method`` — how it arrives at what it says.
- ``organization`` — who published it, derived from the source registry.
"""

FACET_SEPARATOR = ":"
"""What joins a tag's facet to its name, so a bare tag string still says which
axis it narrows — ``subject:evaluations`` reads as itself wherever it lands."""


def folded(text: str) -> str:
    """``text`` reduced to lowercase words joined by hyphens.

    Both a tag's spelling rule and the way a proposal is matched against the
    declared terms. A judgement writes prose, so ``Subject: Evaluations`` and
    ``subject:evaluations`` arrive as one intent spelled two ways; folding both
    sides is what keeps a free tag a genuine gap in the vocabulary rather than
    a capitalisation of something already in it.
    """
    spaced = "".join(char if char.isalnum() else " " for char in text)
    return "-".join(word.lower() for word in spaced.split())


class TagTerm(BaseModel):
    """One tag a browse can filter on, and what it covers.

    ``description`` is not decoration: it is what a judgement reads to decide
    whether the tag applies, so a term described vaguely produces tags that
    mean whatever the judgement felt like that document.
    """

    model_config = ConfigDict(frozen=True)

    facet: TagFacet = Field(description="Which axis this tag narrows")
    name: str = Field(description="The tag's name within its facet")
    description: str = Field(description="What this tag covers, in a sentence")

    @computed_field(description="The tag as it is recorded and filtered on")
    @property
    def tag(self) -> str:
        """The full tag, composed rather than spelled — so it cannot disagree
        with the facet it is declared under."""
        return f"{self.facet}{FACET_SEPARATOR}{self.name}"


ALIGNMENT = TagTerm(
    facet="subject",
    name="alignment",
    description=(
        "Making a system pursue what its developers intended, and the failures "
        "where it does not — reward hacking, deception, sycophancy, scheming"
    ),
)
INTERPRETABILITY = TagTerm(
    facet="subject",
    name="interpretability",
    description=(
        "Reading a model's internals to explain what it computes — features, "
        "circuits, probes, activation steering"
    ),
)
EVALUATIONS = TagTerm(
    facet="subject",
    name="evaluations",
    description=(
        "Measuring what a system can do or is disposed to do, and what makes "
        "such a measurement trustworthy"
    ),
)
DANGEROUS_CAPABILITIES = TagTerm(
    facet="subject",
    name="dangerous-capabilities",
    description=(
        "Capabilities whose misuse is the concern — biological and chemical "
        "uplift, cyber offence, autonomous replication, manipulation"
    ),
)
SECURITY = TagTerm(
    facet="subject",
    name="security",
    description=(
        "Protecting weights, infrastructure, and deployments from an attacker, "
        "including adversarial attacks on the model itself"
    ),
)
GOVERNANCE = TagTerm(
    facet="subject",
    name="governance",
    description=(
        "Policy, regulation, standards, treaties, and the institutions that set "
        "them, including how a lab governs itself"
    ),
)
COMPUTE = TagTerm(
    facet="subject",
    name="compute",
    description=(
        "Hardware, training runs, and the scaling of both — chips, clusters, "
        "compute trends, and their supply"
    ),
)
ECONOMICS = TagTerm(
    facet="subject",
    name="economics",
    description=(
        "Labour, growth, diffusion, and market effects of AI, including what "
        "deployment costs and who captures the value"
    ),
)
FORECASTING = TagTerm(
    facet="subject",
    name="forecasting",
    description=(
        "Timelines, trend extrapolation, and explicit predictions about how "
        "capability or adoption develops"
    ),
)
MODEL_RELEASE = TagTerm(
    facet="subject",
    name="model-release",
    description=(
        "A particular system's release and what shipped with it — capabilities, "
        "safeguards, deployment terms"
    ),
)
AGENTS = TagTerm(
    facet="subject",
    name="agents",
    description=(
        "Systems that act over long horizons with tools, and how their autonomy "
        "is bounded and overseen"
    ),
)

DEFAULT_SUBJECTS: tuple[TagTerm, ...] = (
    ALIGNMENT,
    INTERPRETABILITY,
    EVALUATIONS,
    DANGEROUS_CAPABILITIES,
    SECURITY,
    GOVERNANCE,
    COMPUTE,
    ECONOMICS,
    FORECASTING,
    MODEL_RELEASE,
    AGENTS,
)
"""What the corpus is tracked for. Declared as terms rather than strings, so a
source declaring one of them names the term and a rename reaches every use."""


EMPIRICAL_STUDY = TagTerm(
    facet="method",
    name="empirical-study",
    description="New measurements or experiments on real systems",
)
BENCHMARK = TagTerm(
    facet="method",
    name="benchmark",
    description="Introduces or reports results on a benchmark or evaluation suite",
)
RED_TEAM = TagTerm(
    facet="method",
    name="red-team",
    description="Adversarial testing of a specific system, looking for what breaks it",
)
THEORY = TagTerm(
    facet="method",
    name="theory",
    description="A formal or conceptual argument, with no new measurement of its own",
)
SURVEY = TagTerm(
    facet="method",
    name="survey",
    description="Synthesizes existing work or maps a landscape rather than adding to it",
)
DATASET = TagTerm(
    facet="method",
    name="dataset",
    description="Releases or documents data other people can reuse",
)
POLICY_ANALYSIS = TagTerm(
    facet="method",
    name="policy-analysis",
    description="Weighs a policy option, legal instrument, or regulatory text",
)
POSITION = TagTerm(
    facet="method",
    name="position",
    description="Argues a stance or makes recommendations rather than reporting a result",
)
SYSTEM_CARD = TagTerm(
    facet="method",
    name="system-card",
    description="First-party documentation shipped with a released system",
)
ANNOUNCEMENT = TagTerm(
    facet="method",
    name="announcement",
    description=(
        "News, hiring, or organizational update rather than research — worth "
        "tagging so a browse can leave it out"
    ),
)

DEFAULT_METHODS: tuple[TagTerm, ...] = (
    EMPIRICAL_STUDY,
    BENCHMARK,
    RED_TEAM,
    THEORY,
    SURVEY,
    DATASET,
    POLICY_ANALYSIS,
    POSITION,
    SYSTEM_CARD,
    ANNOUNCEMENT,
)
"""How a document arrives at what it says. Orthogonal to subject on purpose: a
browse narrowing to ``subject:evaluations`` and ``method:position`` is asking a
different question from one narrowing to ``method:empirical-study``."""


def organization_term(key: str, organization: str) -> TagTerm:
    """The organization tag for one declared source.

    Named by the source key rather than by the organization's prose name: the
    key is already the corpus directory and is already unique, so a browse
    filters on ``organization:metr`` rather than on a slug of "METR" that only
    happens to match.
    """
    return TagTerm(facet="organization", name=key, description=organization)


SIGNATURE_CHARS = 12
"""How much of the vocabulary digest is kept. Long enough that two vocabularies
do not collide, short enough to read in an index or a report."""


class TagVocabulary(BaseModel):
    """Every tag a browse may filter on, by facet.

    Subjects and methods are what a judgement chooses from, since only the
    document itself can settle them. Organizations are not offered to a
    judgement at all — the registry already knows who published a document, and
    asking a model to re-derive a declared fact is how declarations start
    disagreeing with the corpus that came from them.
    """

    model_config = ConfigDict(frozen=True)

    subjects: tuple[TagTerm, ...] = DEFAULT_SUBJECTS
    methods: tuple[TagTerm, ...] = DEFAULT_METHODS
    organizations: tuple[TagTerm, ...] = ()

    def terms(self) -> tuple[TagTerm, ...]:
        """Every declared term — what a browse surface can offer."""
        return (*self.subjects, *self.methods, *self.organizations)

    def judged_terms(self) -> tuple[TagTerm, ...]:
        """The terms only the document can settle, which a judgement picks from."""
        return (*self.subjects, *self.methods)

    def tags(self) -> tuple[str, ...]:
        """Every declared tag, sorted."""
        return tuple(sorted(term.tag for term in self.terms()))

    def holds(self, tag: str) -> bool:
        """Whether ``tag`` is declared, and so belongs in a document's core."""
        return any(term.tag == tag for term in self.terms())

    def resolve(self, proposed: str) -> TagTerm | None:
        """The declared term ``proposed`` names, however it was spelled.

        Matching on the folded spelling rather than the literal string, so a
        judgement that answers "Subject: Evaluations" lands in ``core`` instead
        of arriving as a free tag that duplicates a declared one.
        """
        key = folded(proposed)
        return next((term for term in self.terms() if folded(term.tag) == key), None)

    def signature(self) -> str:
        """A digest of what this vocabulary means, descriptions included.

        Recorded on every document a judgement tags, so a re-tag can tell which
        documents predate an edit. Descriptions count because they are what a
        judgement reads: rewording a term changes which documents it catches
        just as surely as adding one does.
        """
        written = "\n".join(
            sorted(f"{term.tag}\t{term.description}" for term in self.terms())
        )
        return hashlib.sha256(written.encode()).hexdigest()[:SIGNATURE_CHARS]


DEFAULT_VOCABULARY = TagVocabulary()
"""The declared core, before the registry's organizations are folded in.

Overridable rather than fixed: a caller narrowing the corpus to one question
builds its own ``TagVocabulary``, and every path below takes one as an
argument, so nothing here has to be edited to be disagreed with.
"""


class DocumentTags(BaseModel):
    """What one document is tagged with, and what decided each tag.

    Kept split by provenance rather than merged into one list, because the two
    halves fail differently. ``derived`` comes from the source declaration and
    is true of the document by construction; ``core`` and ``free`` come from
    reading the document, and can be wrong, thin, or worth reviewing.

    That split is also what makes a gap visible. Every document of a declared
    source carries its organization, so a merged list would never be empty and
    "nothing about this document applied" would look exactly like a document
    that was tagged well — which is precisely the case a browse needs to see.
    """

    model_config = ConfigDict(frozen=True)

    derived: tuple[str, ...] = Field(
        default=(),
        description="Tags the source declaration settles, never re-judged per document",
    )
    core: tuple[str, ...] = Field(
        default=(),
        description="Declared-vocabulary tags this document earned by what it says",
    )
    free: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tags proposed outside the vocabulary, kept for review and promotion "
            "rather than offered as a filter"
        ),
    )
    judged: bool = Field(
        default=False,
        description=(
            "Whether the per-document pass ran. False means nobody has looked "
            "yet, which is a different gap from having looked and found nothing"
        ),
    )
    vocabulary: str = Field(
        default="",
        description="Signature of the vocabulary the judgement was made against",
    )
    assigned_at: str = Field(default="", description="When these tags were assigned")

    def applied(self) -> tuple[str, ...]:
        """Every tag on this document, whatever decided it."""
        return tuple(sorted({*self.derived, *self.core, *self.free}))

    def carries(self, tag: str) -> bool:
        """Whether a browse filtering on ``tag`` should return this document."""
        return tag in self.applied()

    def untagged(self) -> bool:
        """Looked at, and nothing about the document itself applied.

        A real gap in coverage: the document is reachable by who published it
        and by nothing else, so every subject browse walks past it.
        """
        return self.judged and not self.core and not self.free

    def current(self, vocabulary: TagVocabulary) -> bool:
        """Whether these tags were judged against ``vocabulary`` as it stands."""
        return self.judged and self.vocabulary == vocabulary.signature()
