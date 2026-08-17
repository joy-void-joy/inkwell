"""Whether a citation set leans — over provenance, and over position.

Once a source records where it was published, when, by whom, and in what venue,
two questions become answerable that no single citation answers alone. The first
is mechanical: how a section's citations, and the whole work's, distribute over
domain, organization, author, venue, and date. The second is not mechanical at
all — for a claim that is actually argued over, whether every source cited for it
sits on the same side, which no distribution over hosts can see. Ten domains can
be ten voices in one camp.

Not leaning on one venue is a claim about the finished thing, and where the
finished thing is a book, one run is one chapter of it. So the distribution
also measures the book: every citation every chapter recorded, read off the
records those chapters left rather than their research, against ceilings of its
own, because the same fraction is a stricter demand over nine chapters' sources
than over one chapter's. The judged pass does not widen — a claim is argued in
the chapter that makes it, and its citation set is whole there.

So there are two checks. The distribution counts what research already recorded
and costs nothing, so it re-runs on every draft. The judged pass spends a
reviewer, and only on the claims :class:`ContestedSignal` marks — research's own
confidence and how many sources it stacked behind an answer, read off the record
rather than a model re-deciding which claims are arguable each time it runs.

Both are advisory. They produce :class:`CheckRow`s, which the rewrite reads
beside its review findings and which never fail a stage: citations leaning on one
domain is a fact about the draft, not a defect that should stop a run, and the
stage that can do something about it is the one rewriting the prose. A missing
side is the one row worth the author's attention, so it also becomes a note on
their document.

Nothing here re-derives provenance. Every axis reads a field the researcher
recorded, so a section whose sources carry none reports as unknown rather than as
several distinct values that look like diversity.
"""

from collections import Counter
from collections.abc import Iterator
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from inkwell.agent.book import BookCitations
from inkwell.agent.models import (
    ArticlePlan,
    AuthorNote,
    ResearchCompilation,
    ResearchFinding,
    ResearchSource,
    SectionDraft,
)
from inkwell.agent.provenance import SourceProvenance

WHOLE_WORK = "the whole work"
"""The scope a row carries when it is about every citation this run made.

The whole of what one run writes, which for a run placed in a book is one
chapter of it. :func:`book_scope` names the wider one.
"""

WHOLE_BOOK = "the whole book"
"""What the scope of a row about every chapter's citations starts with."""


def book_scope(book: str) -> str:
    """The scope a row about a whole book names.

    Distinguishable from :data:`WHOLE_WORK` on purpose, and carrying which book
    it is: a rewrite reading the rows has to be able to tell "this chapter
    leans" — which is its to fix — from "the book leans", which is not.
    """
    return f"{WHOLE_BOOK} — {book}"


DIVERSITY_STAGE = "review:diversity"
"""The stage an author-visible diversity note is attributed to."""


class ProvenanceAxis(BaseModel):
    """One axis a citation set can lean on, and how much may sit on one value.

    Each axis reads its own recorded field, so measuring a new one is a class and
    an entry in :data:`CITATION_AXES` rather than an edit to the counting. A
    source whose provenance was never recorded contributes nothing to any axis,
    which is what makes an unrecorded set report as unknown instead of as a
    handful of distinct values that read as spread.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="What this axis is called in a row")
    share_ceiling: float = Field(
        default=0.5,
        description=(
            "How much of a citation set may sit on one value before a row "
            "reports it, as a fraction of the citations that recorded one"
        ),
    )

    def values(self, recorded: SourceProvenance | None) -> list[str]:
        """What one citation contributes, empty where nothing was recorded."""
        return self.recorded_values(recorded) if recorded is not None else []

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        """The values one recorded block carries on this axis, empty for silence."""
        return []


class DomainAxis(ProvenanceAxis):
    """The host the document was served from, as the acquisition reported it."""

    name: str = "domain"

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        return [recorded.domain] if recorded.domain else []


class OrganizationAxis(ProvenanceAxis):
    """Who published the document — the journal, lab, agency, or outlet."""

    name: str = "organization"

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        return [recorded.organization] if recorded.organization else []


class AuthorAxis(ProvenanceAxis):
    """Who wrote it. One source contributes every name on its byline."""

    name: str = "author"

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        return [name for name in recorded.authors if name]


class VenueAxis(ProvenanceAxis):
    """What kind of thing published it. An unknown venue is silence, not a value."""

    name: str = "venue"
    share_ceiling: float = 0.7

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        return [recorded.venue] if recorded.venue != "unknown" else []


class YearAxis(ProvenanceAxis):
    """When it was published. An undated source records no year to count."""

    name: str = "date"
    share_ceiling: float = 0.7

    def recorded_values(self, recorded: SourceProvenance) -> list[str]:
        year = recorded.year()
        return [year] if year else []


CITATION_AXES: list[ProvenanceAxis] = [
    DomainAxis(),
    OrganizationAxis(),
    AuthorAxis(),
    VenueAxis(),
    YearAxis(),
]
"""The axes a citation set is measured on.

Venue and date carry looser ceilings than the rest: a piece about one year's
results is supposed to cite that year, and a technical argument leaning on
preprints is a normal shape rather than a lapse. Leaning on one domain, one
organization, or one author is not.
"""

BOOK_CITATION_AXES: list[ProvenanceAxis] = [
    DomainAxis(share_ceiling=0.35),
    OrganizationAxis(share_ceiling=0.35),
    AuthorAxis(share_ceiling=0.35),
    VenueAxis(share_ceiling=0.5),
    YearAxis(share_ceiling=0.5),
]
"""The axes a whole book is measured on — the same axes, tighter ceilings.

One fraction is two different demands at the two scopes. A chapter about one
year's results citing that year is the shape of its subject; a whole book
sitting there is the shape of its research. And spread costs less the more
citations there are to spread over, so what one chapter may sit at, nine
together should not.
"""


class ContestedSignal(BaseModel):
    """Which claims a judge is spent on, read off the record rather than guessed.

    Two signals research already wrote down: the confidence it reported, and how
    many sources it stacked behind an answer. A claim it was unsure of, or that
    took several citations to support, is where every source sitting on one side
    can hide — a confident single-source fact is not worth a reviewer. Declared
    here rather than decided per run, so which claims get judged is a property of
    the data and the same twice in a row.
    """

    model_config = ConfigDict(frozen=True)

    confidence_below: float = Field(
        default=0.75,
        description=(
            "Research confidence under which a claim is worth judging — what "
            "research was unsure of is what a one-sided citation set explains"
        ),
    )
    several_citations: int = Field(
        default=3,
        description=(
            "How many sources make a claim worth judging whatever its "
            "confidence: stacking citations is what defending a position "
            "looks like"
        ),
    )

    def contests(self, finding: ResearchFinding) -> bool:
        """Whether this finding's citation set is worth judging for one-sidedness."""
        if not finding.sources:
            return False
        return (
            finding.confidence < self.confidence_below
            or len(finding.sources) >= self.several_citations
        )


class DiversityRules(BaseModel):
    """What the checks measure and the thresholds they report against.

    Passed in rather than reached for, so a caller with different standards — a
    literature review that should lean on one venue, a piece where two
    organizations are the whole field — hands over its own rather than editing
    this module's.
    """

    model_config = ConfigDict(frozen=True)

    axes: list[ProvenanceAxis] = Field(
        default=CITATION_AXES,
        description="The axes one run's own citation sets are measured on",
    )
    book_axes: list[ProvenanceAxis] = Field(
        default=BOOK_CITATION_AXES,
        description=(
            "The axes every chapter's citations together are measured on, "
            "separate from the run's own because the same share is a "
            "different demand over a book than over one chapter of it"
        ),
    )
    min_citations: int = Field(
        default=3,
        description=(
            "Below this many citations carrying a value, a leading share is "
            "small numbers rather than a lean — two sources always look like half"
        ),
    )
    contested: ContestedSignal = Field(
        default=ContestedSignal(),
        description="Which claims the judged pass is spent on",
    )


DEFAULT_DIVERSITY_RULES = DiversityRules()
"""The standards the pipeline measures against unless a caller passes its own."""


class CheckRow(BaseModel):
    """One advisory row: which check spoke, about what, and what it found.

    A row is a report, never a verdict a stage can fail on. It names its scope so
    a rewrite can tell a section's problem from its chapter's, and both from the
    book's — a lean nine chapters share is not one chapter's to fix — and carries
    an anchor where there is draft text a comment could attach to.
    """

    check: str = Field(description="Which check produced this row")
    scope: str = Field(description="Section title, the whole work, or a whole book")
    detail: str = Field(description="What it found, in terms a rewrite can act on")
    anchor: str = Field(
        default="",
        description="Claim or draft text the row is about, for anchoring a comment",
    )
    missing_side: str = Field(
        default="",
        description=(
            "The position no cited source takes, where a judged row found the "
            "set one-sided; empty on a computed row"
        ),
    )

    def render_line(self) -> str:
        """This row as one line of the advisory report."""
        side = f" Missing side: {self.missing_side}" if self.missing_side else ""
        return f"- [{self.scope}] {self.check}: {self.detail}{side}"

    def author_note(self) -> AuthorNote:
        """This row as a note on the author's document."""
        return AuthorNote(
            note=(
                f"{self.check} — {self.scope}. {self.detail} "
                f"Missing side: {self.missing_side}"
            ),
            anchor=self.anchor,
            stage=DIVERSITY_STAGE,
        )


class ValueShare(BaseModel):
    """One value on an axis, and how many citations sit on it."""

    value: str = Field(description="The recorded value")
    count: int = Field(description="Citations carrying it")


class AxisTally(BaseModel):
    """How one citation set distributes over one axis.

    ``recorded`` counts the citations that carried a value and ``unrecorded``
    those that carried none, kept apart because they answer different questions:
    a set that leans and a set nobody recorded provenance for both hold one
    value's worth of information, and only the first is about the citations.
    """

    axis: str = Field(description="Which axis this counts")
    scope: str = Field(description="Section title, the whole work, or a whole book")
    recorded: int = Field(description="Citations that carried a value on this axis")
    unrecorded: int = Field(description="Citations that carried none")
    share_ceiling: float = Field(
        description="The fraction this tally was measured against"
    )
    shares: list[ValueShare] = Field(
        description="Each value and its count, most cited first"
    )

    def leading(self) -> ValueShare | None:
        """The value the most citations sit on, or nothing where none recorded one."""
        return self.shares[0] if self.shares else None

    def share(self) -> float:
        """What fraction of the citations carrying a value sit on the leading one."""
        leader = self.leading()
        if leader is None or self.recorded == 0:
            return 0.0
        return leader.count / self.recorded

    def silent(self) -> bool:
        """Whether nothing was recorded here, so the axis says nothing about spread."""
        return self.recorded == 0 and self.unrecorded > 0

    def leans(self, min_citations: int) -> bool:
        """Whether enough citations carried a value, and too many share one."""
        return self.recorded >= min_citations and self.share() > self.share_ceiling

    def render_line(self) -> str:
        """This tally as one line of the distribution table."""
        if self.silent():
            return (
                f"- [{self.scope}] {self.axis}: nothing recorded for "
                f"{self.unrecorded} citation(s) — unknown, not spread"
            )
        leader = self.leading()
        lead = (
            f", led by {leader.value} ({leader.count}, {self.share():.0%})"
            if leader is not None
            else ""
        )
        trailing = f", {self.unrecorded} unrecorded" if self.unrecorded else ""
        return (
            f"- [{self.scope}] {self.axis}: {len(self.shares)} value(s) over "
            f"{self.recorded} citation(s){lead}{trailing}"
        )

    def row(self, min_citations: int) -> CheckRow | None:
        """What this tally reports, or nothing where it has nothing to say."""
        if self.silent():
            return CheckRow(
                check=f"{self.axis} unrecorded",
                scope=self.scope,
                detail=(
                    f"None of the {self.unrecorded} citation(s) here recorded a "
                    f"{self.axis}, so nothing says whether they are diverse. "
                    f"This is unknown, not spread."
                ),
            )
        leader = self.leading()
        if leader is None or not self.leans(min_citations):
            return None
        return CheckRow(
            check=f"{self.axis} concentration",
            scope=self.scope,
            detail=(
                f"{leader.count} of {self.recorded} citation(s) carrying a "
                f"{self.axis} sit on {leader.value} ({self.share():.0%}, over "
                f"the {self.share_ceiling:.0%} this axis reports at)."
            ),
        )


class CitationSet(BaseModel):
    """The citations one scope makes — a section, one run's whole work, a book.

    Each citation is what research recorded about it, and nothing where it
    recorded none: every axis reads a recorded field, so the provenance is the
    whole of what a distribution can see. That is also what lets a scope be
    assembled from records a run has already written rather than from the
    research they were read off.

    A source cited by two findings counts twice: the question is how the citation
    mass distributes, and a document leaned on twice is leaned on twice.
    """

    scope: str = Field(description="Section title, the whole work, or a whole book")
    citations: list[SourceProvenance | None] = Field(
        description=(
            "What research recorded about each citation in this scope, and "
            "None for each citation it recorded nothing about"
        )
    )

    def tally(self, axis: ProvenanceAxis) -> AxisTally:
        """How these citations distribute over one axis."""
        counted = Counter(
            value for recorded in self.citations for value in axis.values(recorded)
        )
        unrecorded = sum(1 for recorded in self.citations if not axis.values(recorded))
        return AxisTally(
            axis=axis.name,
            scope=self.scope,
            recorded=len(self.citations) - unrecorded,
            unrecorded=unrecorded,
            share_ceiling=axis.share_ceiling,
            shares=[
                ValueShare(value=value, count=count)
                for value, count in sorted(
                    counted.items(), key=lambda counted: (-counted[1], counted[0])
                )
            ],
        )

    def tallies(self, axes: list[ProvenanceAxis]) -> list[AxisTally]:
        """How these citations distribute over each of these axes.

        Which axes is the caller's, because it is the caller that knows what
        scope this is: a book is measured on ceilings a chapter is not.
        """
        return [self.tally(axis) for axis in axes]


class OneSidedVerdict(BaseModel):
    """A judge's answer on one contested claim's citation set."""

    one_sided: bool = Field(
        description=(
            "True when every source cited for this claim argues the same side of "
            "it. False when the set includes a source taking a different "
            "position, or when the claim is not one people take sides on."
        )
    )
    missing_side: str = Field(
        default="",
        description=(
            "The position no cited source takes, in one specific sentence — who "
            "holds it and on what grounds, so a writer can go and find it. "
            "Required when one_sided is true."
        ),
    )
    reason: str = Field(
        default="",
        description="Why the cited set reads as one-sided, in one sentence",
    )

    @model_validator(mode="after")
    def one_sided_names_the_missing_side(self) -> Self:
        """A one-sided verdict naming no missing side leaves nothing to act on."""
        if self.one_sided and not self.missing_side.strip():
            raise ValueError(
                "one_sided=True requires missing_side — name the position none "
                "of the cited sources takes. If you cannot name one, the set is "
                "not one-sided and one_sided is False."
            )
        return self


class ContestedClaim(BaseModel):
    """One claim the signal marked, and the citation set a judge has to weigh."""

    scope: str = Field(description="Section the claim was researched for")
    finding: ResearchFinding = Field(description="The claim and every source cited")

    def render(self) -> str:
        """The claim and its citations, as the judge reads them."""

        def lines() -> Iterator[str]:
            """The brief, one element per line."""
            yield f"Claim: {self.finding.question}"
            yield f"Research answer: {self.finding.answer}"
            yield f"Research confidence: {self.finding.confidence:.2f}"
            yield f"Sources cited for it ({len(self.finding.sources)}):"
            for source in self.finding.sources:
                recorded = source.provenance
                described = (
                    f"{recorded.venue}, {recorded.organization or 'unknown org'}, "
                    f"{recorded.year() or 'undated'}"
                    if recorded is not None
                    else "provenance unrecorded"
                )
                yield f"- {source.title} ({described}) {source.url}"
                yield f"  Excerpt: {source.key_excerpt}"

        return "\n".join(lines())


class JudgedClaim(BaseModel):
    """One contested claim and what the judge said about its citation set."""

    claim: ContestedClaim = Field(description="What was judged")
    verdict: OneSidedVerdict = Field(description="What the judge found")

    def row(self) -> CheckRow | None:
        """What this judgement reports, or nothing where the set was not one-sided."""
        if not self.verdict.one_sided:
            return None
        return CheckRow(
            check="position diversity",
            scope=self.claim.scope,
            detail=(
                f"Every source cited for “{self.claim.finding.question}” argues "
                f"the same side. {self.verdict.reason}"
            ).rstrip(),
            anchor=self.claim.finding.question,
            missing_side=self.verdict.missing_side,
        )


class DiversityReport(BaseModel):
    """Every advisory row the two checks produced, and the counts behind them.

    Advisory by construction: this carries rows and renders them, and has no way
    to fail a stage. The rewrite reads what it renders; the rows naming a missing
    side become notes on the author's document.
    """

    tallies: list[AxisTally] = Field(
        default_factory=list, description="The distribution, per scope and axis"
    )
    rows: list[CheckRow] = Field(
        default_factory=list, description="What the checks found worth reporting"
    )
    judged: int = Field(
        default=0, description="How many contested claims the judged pass weighed"
    )

    def author_notes(self) -> list[AuthorNote]:
        """The rows worth the author's attention — the ones naming a missing side."""
        return [row.author_note() for row in self.rows if row.missing_side]

    def render(self) -> str:
        """The report the rewrite stage reads."""

        def lines() -> Iterator[str]:
            """The report, section by section."""
            yield "# Citation diversity (advisory)"
            yield ""
            yield (
                "Nothing here blocks anything. It reports how the citations "
                "distribute over what research recorded about each source, and "
                "whether the sources cited for a contested claim all argue the "
                "same side. Act on what prose can act on: attribute a lean where "
                "it stands, or say plainly that a claim rests on one camp."
            )
            yield ""
            yield "## What the checks found"
            yield ""
            if self.rows:
                yield from (row.render_line() for row in self.rows)
            else:
                yield "- Nothing to report."
            yield ""
            yield f"## Distribution ({len(self.tallies)} tallies)"
            yield ""
            yield from (tally.render_line() for tally in self.tallies)
            yield ""
            yield "## Position"
            yield ""
            one_sided = sum(1 for row in self.rows if row.missing_side)
            yield (
                f"- {self.judged} contested claim(s) judged, {one_sided} found "
                f"one-sided."
            )

        return "\n".join(lines())


def section_citations(
    section_title: str,
    plan: ArticlePlan,
    research: ResearchCompilation,
    draft: SectionDraft | None,
) -> list[ResearchSource]:
    """The citations one section makes.

    Two recorded links, unioned rather than ranked: the plan says which research
    questions were asked on this section's behalf, and the section's own draft
    says which sources it referenced. Either alone misses citations — the plan
    link exists before a draft does, and a writer can reach for a sibling
    section's source — so a citation counts for the section if either says so.
    """
    asked = {
        question.question.casefold()
        for question in plan.research_questions
        if question.section.casefold() == section_title.casefold()
    }
    used = {url.casefold() for url in (draft.sources_used if draft is not None else [])}

    def answers(finding: ResearchFinding) -> bool:
        """Whether research asked this question on the section's behalf."""
        question = finding.question.casefold()
        return any(asked_of in question or question in asked_of for asked_of in asked)

    def cited() -> Iterator[ResearchSource]:
        """Each citation the section owns, once per finding that made it."""
        for finding in research.findings:
            for_section = answers(finding)
            for source in finding.sources:
                if for_section or source.url.casefold() in used:
                    yield source

    return list(cited())


def recorded_citations(research: ResearchCompilation) -> list[SourceProvenance | None]:
    """What research recorded about every citation it made, in the order made.

    The whole of what any distribution over this run can see, and the whole of
    what a chapter has to leave behind for its book to be measured. ``None``
    where nothing was recorded rather than a shorter list, so the citations
    nobody characterized stay counted as citations.
    """
    return [
        source.provenance for finding in research.findings for source in finding.sources
    ]


def citation_sets(
    research: ResearchCompilation,
    *,
    plan: ArticlePlan | None = None,
    drafts: dict[str, SectionDraft] | None = None,
) -> list[CitationSet]:
    """This run's whole citation set, and one per planned section.

    The whole work is every citation every finding made, which stands on its own
    before there is a plan to attribute them to. Where the run is one chapter of
    a book, this is the chapter's own; the book's is :func:`book_citation_set`.
    """
    written = drafts if drafts is not None else {}
    whole = CitationSet(scope=WHOLE_WORK, citations=recorded_citations(research))
    if plan is None:
        return [whole]

    def for_section(title: str) -> CitationSet:
        """One section's citations, with its draft where one has been written."""
        return CitationSet(
            scope=title,
            citations=[
                source.provenance
                for source in section_citations(
                    title,
                    plan,
                    research,
                    written[title] if title in written else None,
                )
            ],
        )

    return [whole, *(for_section(section.title) for section in plan.sections)]


def book_citation_set(book: BookCitations) -> CitationSet:
    """Every citation every chapter of one book recorded, as one scope.

    Assembled from what the chapters left behind rather than from what they
    researched, which is what lets the widest scope cost what the narrowest
    does. A chapter that has not run yet left no record, so it contributes
    nothing rather than counting as a chapter that cited nothing.
    """
    return CitationSet(scope=book_scope(book.book), citations=book.citations())


def distribution_report(
    research: ResearchCompilation,
    *,
    plan: ArticlePlan | None = None,
    drafts: dict[str, SectionDraft] | None = None,
    book: BookCitations | None = None,
    rules: DiversityRules = DEFAULT_DIVERSITY_RULES,
) -> DiversityReport:
    """The computed half: how the citations distribute, per section, run, and book.

    Reads only what research recorded, so it costs nothing and re-runs on every
    draft — the book scope reads the records its chapters left as they ran, so
    widening the scope does not widen the cost. A run given no book measures
    exactly its own scopes. A scope with no citations at all is left out —
    there is a difference between citations nobody characterized and no
    citations.
    """

    def measured() -> Iterator[AxisTally]:
        """Every tally, widest scope first, each on the axes its scope answers to."""
        whole_book = book_citation_set(book) if book is not None else None
        if whole_book is not None and whole_book.citations:
            yield from whole_book.tallies(rules.book_axes)
        for citations in citation_sets(research, plan=plan, drafts=drafts):
            if citations.citations:
                yield from citations.tallies(rules.axes)

    tallies = list(measured())
    return DiversityReport(
        tallies=tallies,
        rows=[
            row
            for row in (tally.row(rules.min_citations) for tally in tallies)
            if row is not None
        ],
    )


def contested_claims(
    research: ResearchCompilation,
    *,
    plan: ArticlePlan | None = None,
    rules: DiversityRules = DEFAULT_DIVERSITY_RULES,
) -> list[ContestedClaim]:
    """The claims worth judging for one-sidedness, and which section each serves."""

    def scope_of(finding: ResearchFinding) -> str:
        """The section this claim was researched for, or the whole work."""
        if plan is None:
            return WHOLE_WORK
        question = finding.question.casefold()
        for asked in plan.research_questions:
            asked_of = asked.question.casefold()
            if asked_of in question or question in asked_of:
                return asked.section or WHOLE_WORK
        return WHOLE_WORK

    return [
        ContestedClaim(scope=scope_of(finding), finding=finding)
        for finding in research.findings
        if rules.contested.contests(finding)
    ]


def judged_report(judged: list[JudgedClaim]) -> DiversityReport:
    """The judged half: a row per claim whose cited sources all sit on one side."""
    return DiversityReport(
        rows=[row for row in (claim.row() for claim in judged) if row is not None],
        judged=len(judged),
    )


def merge_reports(
    computed: DiversityReport, judged: DiversityReport
) -> DiversityReport:
    """Both checks as one report, judged rows first.

    A missing side outranks a lean, because it is the one a reader would notice.
    """
    return DiversityReport(
        tallies=computed.tallies + judged.tallies,
        rows=judged.rows + computed.rows,
        judged=computed.judged + judged.judged,
    )
