"""Checks a format declares beside its guidance.

A format's guidance used to be advice: prose a writer may or may not have
followed, and nothing re-read a draft against. Most of it is measurable, so a
format declares its rules here as rows instead — in the shape `dev check`
reports, named and advisory, and read by the rewrite stage for the draft it is
about to revise.

Two tiers, one row shape. Mechanical rows are data: a threshold and the
guidance sentence they measure, re-run on every draft for nothing. Judged rows
spend a reviewer on what counting cannot settle — whether a passage is
concrete enough to teach from — and land their verdict in that same row, so
the rewrite stage reads one report rather than two.

Adding a row to a format is a constructor call in that format's declaration.
Adding a *kind* of row is one class here, and no edit to any report, renderer,
or stage: the base declares `run` and each row answers it.

A row need not belong to a format at all. One that measures something about
what the run *is* rather than what it is written in — a chapter of a book has
references into that book, whatever format the chapter takes — is constructed
per run and handed to the same check, so it reaches every format without being
named by any, and stays silent by being absent rather than by declaring
nothing.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from inkwell.agent.book import (
    BookAddress,
    BookRecord,
    ChapterAddress,
    ChapterPlacement,
    SectionAddress,
    placed_sections,
)
from inkwell.agent.book_links import LINK_SCHEME, BookReferences, UnresolvedReference
from inkwell.agent.config import PipelineStage, stage_model
from inkwell.agent.markdown_to_docs import markdown_to_requests
from inkwell.agent.prose import Paragraph, Prose, ProseReader, Sentence, spread

logger = logging.getLogger(__name__)

ADVISORY_PREAMBLE = """\
These rows are advisory. They report; they do not gate this stage, and a row \
that fired is not on its own a reason to change anything. The author's voice \
profile and the plan's voice_notes outrank every row below: where a row fires \
against how the author actually writes — their rhythm, their asides, their \
hedges, their self-implication — the author wins and the row is noise. Act on \
a row only where acting on it also makes the passage better."""
"""Travels at the head of every report, so no stage reads the rows without the
precedence that decides what to do when one fires against the author."""


class Verdict(BaseModel):
    """A reviewer's answer about a draft, in the shape a row reports."""

    holds: bool = Field(description="Whether the draft meets what the row asks")
    measured: str = Field(
        description="One line on what was looked at and concluded, so a reader "
        "sees the finding and not only the verdict"
    )
    findings: list[str] = Field(
        default_factory=list,
        description="The specific passages that fall short, empty when none do",
    )


class CheckRow(BaseModel):
    """One named check's result — the row a report prints.

    A mechanical row and a judged row both land here, which is what lets the
    rewrite stage read one report instead of learning two shapes.
    """

    name: str = Field(description="Row name, as the report prints it")
    measured: str = Field(
        description="What the row counted, so a reader sees the number rather "
        "than only the verdict"
    )
    fired: bool = Field(
        default=False,
        description="Whether what was measured falls outside what the format "
        "declared. Never gating — a fired row is a line in a report",
    )
    findings: list[str] = Field(
        default_factory=list,
        description="The concrete places the row is about",
    )

    def render(self) -> Iterator[str]:
        """This row as `dev check` prints one: a verdict line, then detail."""
        state = f"{len(self.findings)} finding(s) (advisory)" if self.fired else "ok"
        yield f"{self.name}: {state} — {self.measured}"
        if self.fired:
            yield from (f"  {finding}" for finding in self.findings)


class Judge(ABC):
    """Decides a question about a draft that counting cannot settle.

    An injected seam. A row never holds one — it asks the `DraftUnderCheck` it
    was handed, which composes whichever judge this run was built with.
    """

    @abstractmethod
    async def rule(self, question: str, draft_path: Path) -> Verdict:
        """The reviewer's verdict on one question about one draft."""


class DraftUnderCheck:
    """The draft a row is checked against, and what a row may spend on it.

    Composes the segmenter and the reviewer seams so a row reads parsed prose
    and asks for a verdict without holding either engine itself.
    """

    def __init__(
        self, prose: Prose, draft_path: Path, judge: Judge, reader: ProseReader
    ) -> None:
        self.prose = prose
        self.draft_path = draft_path
        self.judge = judge
        self.reader = reader

    def phrase(self, text: str) -> list[str]:
        """A declared phrase as the words it is matched by.

        Tokenized by the same segmenter that read the draft, so `crucial`
        matches the word and not the inside of `crucially`.
        """
        return self.reader.phrase(text)

    async def decide(self, question: str) -> Verdict:
        """A reviewer's verdict on something counting cannot decide."""
        return await self.judge.rule(question, self.draft_path)


def contains(words: list[str], phrase: list[str]) -> bool:
    """Whether `phrase` appears as a run of whole words in `words`."""
    if not phrase or len(phrase) > len(words):
        return False
    return any(
        words[start : start + len(phrase)] == phrase
        for start in range(len(words) - len(phrase) + 1)
    )


def share(part: int, whole: int) -> float:
    """`part` out of `whole`, and zero rather than an error when nothing counted."""
    return part / whole if whole else 0.0


class FormatCheck(BaseModel):
    """One check a format declares beside its guidance.

    The base declares `run` and every row answers it, so a new kind of row is
    one class and no edit to any renderer, report, or stage. `rule` is the
    guidance sentence the row measures and is rendered into what the writer
    reads, which is what keeps the rule a writer is given and the rule a draft
    is measured by from drifting apart.

    Pydantic's metaclass is an `ABCMeta`, so `run` binds like any abstract
    method: a kind of row that does not answer it cannot be built.
    """

    name: str = Field(description="Row name, as the report prints it")
    rule: str = Field(
        description="The guidance sentence this row measures, rendered to the "
        "writer so the rule and its check stay one declaration"
    )

    @property
    def mechanical(self) -> bool:
        """Whether counting settles this row, rather than a reviewer.

        Answered by the row itself, so a caller wanting the rows that cost
        nothing to run asks each one instead of matching on a discriminator it
        would have to keep in step with the declarations.
        """
        return True

    @abstractmethod
    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        """This row's verdict on the draft."""

    def result(
        self, measured: str, *, fired: bool, findings: Sequence[str] = ()
    ) -> CheckRow:
        """This row's result, named and carrying what it counted."""
        return CheckRow(
            name=self.name, measured=measured, fired=fired, findings=list(findings)
        )


class BannedVocabulary(FormatCheck):
    """Words and phrases the format's guidance rules out.

    Matched on the segmenter's words rather than on substrings, so a banned
    `crucial` does not fire on `crucially` and a banned phrase has to appear
    as that phrase.
    """

    kind: Literal["banned-vocabulary"] = "banned-vocabulary"
    phrases: list[str] = Field(
        description="The words and phrases this format rules out, as written"
    )
    ceiling: int = Field(
        default=0,
        description="How many uses the format tolerates before the row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        matched = [
            f"“{phrase}” — {sentence.text}"
            for sentence in draft.prose.sentences
            for phrase in self.phrases
            if contains(sentence.words, draft.phrase(phrase))
        ]
        return self.result(
            f"{len(matched)} use(s) of {len(self.phrases)} banned phrase(s), "
            f"{self.ceiling} tolerated",
            fired=len(matched) > self.ceiling,
            findings=matched,
        )


class PunctuationDensity(FormatCheck):
    """How often the draft reaches for a mark it is meant to ration.

    One kind for every mark the author rations — em dashes, semicolons,
    parentheses — because they differ only in which character is counted and
    how many are too many. A format declares one row per mark it cares about,
    and its name is what the report prints.
    """

    kind: Literal["punctuation-density"] = "punctuation-density"
    marks: list[str] = Field(
        description="The marks this row counts, as written. Several together "
        "count as one budget, which is how a paired mark is declared"
    )
    per_thousand_ceiling: float = Field(
        default=4.0,
        description="Marks per thousand words past which the row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        words = draft.prose.length
        found = draft.prose.marks(self.marks)
        density = share(found, words) * 1000
        return self.result(
            f"{found} of {''.join(self.marks)} in {words} words "
            f"({density:.1f} per thousand, ceiling {self.per_thousand_ceiling})",
            fired=density > self.per_thousand_ceiling,
            findings=[
                paragraph.block.plain
                for paragraph in draft.prose.paragraphs
                if sum(paragraph.block.plain.count(mark) for mark in self.marks) > 1
            ],
        )


MARKDOWN_ARTIFACTS = [
    "**",
    "__",
    "##",
    "```",
    "](",
    "~~",
]
"""Markup that means the parser declined to render it. The default for
`MarkdownArtifacts`, which takes an override: which markers count as leakage
depends on what the surface renders."""


class MarkdownArtifacts(FormatCheck):
    """Markup that survived into the prose a reader sees.

    Read after the parser has had its turn: markup still sitting in a block's
    rendered text is markup that did not parse — an unclosed bold run, a
    heading whose hashes have no space after them, a half-written link — rather
    than formatting the reader will see applied. Nothing here guesses at
    syntax; it asks what the parser left behind.
    """

    kind: Literal["markdown-artifacts"] = "markdown-artifacts"
    markers: list[str] = Field(
        default_factory=MARKDOWN_ARTIFACTS.copy,
        description="The markers that count as leakage when they survive the parse",
    )
    ceiling: int = Field(
        default=0, description="How many surviving markers the format tolerates"
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        found = draft.prose.marks(self.markers)
        return self.result(
            f"{found} unrendered marker(s), {self.ceiling} tolerated",
            fired=found > self.ceiling,
            findings=[
                paragraph.block.plain
                for paragraph in draft.prose.paragraphs
                if any(marker in paragraph.block.plain for marker in self.markers)
            ],
        )


class BoldEmphasis(FormatCheck):
    """How much of the draft is set in bold.

    The author's voice guidance rules against boldface overuse, and the
    paragraph-summary formats mandate a bolded sentence opening every
    paragraph. The author already resolved that: bold is permitted where a
    structural format gives it a navigational purpose. So the exemption is
    declared per format here rather than argued at every row — a format whose
    bold carries navigation sets `exempt_paragraph_summaries`, and this row
    then measures only the emphasis its convention did not ask for.
    """

    kind: Literal["bold-emphasis"] = "bold-emphasis"
    per_thousand_ceiling: float = Field(
        default=30.0,
        description="Bolded words per thousand past which the row fires",
    )
    exempt_paragraph_summaries: bool = Field(
        default=False,
        description="Whether a bold run carrying a paragraph's opening summary "
        "is navigation this format asked for, and so uncounted",
    )

    def emphasis(self, paragraph: Paragraph) -> list[str]:
        """The bold runs in a paragraph this row holds against the draft."""
        if self.exempt_paragraph_summaries and paragraph.summary_is_bolded:
            return paragraph.block.bold_runs[1:]
        return paragraph.block.bold_runs

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        runs = [
            run
            for paragraph in draft.prose.paragraphs
            for run in self.emphasis(paragraph)
        ]
        bolded = sum(len(draft.phrase(run)) for run in runs)
        density = share(bolded, draft.prose.length) * 1000
        exempt = (
            " (paragraph summaries exempt)" if self.exempt_paragraph_summaries else ""
        )
        return self.result(
            f"{bolded} bolded word(s) in {draft.prose.length} "
            f"({density:.1f} per thousand, ceiling "
            f"{self.per_thousand_ceiling}){exempt}",
            fired=density > self.per_thousand_ceiling,
            findings=runs,
        )


class FormulaicOpenings(FormatCheck):
    """Paragraphs that keep opening the same way.

    The opening is the shape of a paragraph's first two content words, so
    `Moreover, the …` and `Moreover, a …` read as the same opening — which is
    what a reader notices as a template.
    """

    kind: Literal["formulaic-openings"] = "formulaic-openings"
    repeat_ceiling: int = Field(
        default=2,
        description="How often one opening shape may repeat before the row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        counted = Counter(
            paragraph.opening
            for paragraph in draft.prose.paragraphs
            if paragraph.opening
        )
        repeated = [
            f"“{opening}” opens {count} paragraphs"
            for opening, count in counted.most_common()
            if count > self.repeat_ceiling
        ]
        worst = counted.most_common(1)
        return self.result(
            f"{len(counted)} distinct opening(s) across "
            f"{len(draft.prose.paragraphs)} paragraph(s), most repeated "
            f"{worst[0][1] if worst else 0} time(s), ceiling {self.repeat_ceiling}",
            fired=bool(repeated),
            findings=repeated,
        )


class SentenceLengthVariance(FormatCheck):
    """Whether sentence lengths vary or march in step."""

    kind: Literal["sentence-length-variance"] = "sentence-length-variance"
    spread_floor: float = Field(
        default=4.0,
        description="Standard deviation of sentence word counts below which "
        "the rhythm reads as machine-even and the row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        lengths = [sentence.length for sentence in draft.prose.sentences]
        measured = spread(lengths)
        return self.result(
            f"spread {measured:.1f} words across {len(lengths)} sentence(s), "
            f"floor {self.spread_floor}",
            fired=len(lengths) > 1 and measured < self.spread_floor,
        )


class ParagraphLengthVariance(FormatCheck):
    """Whether paragraph lengths vary or come out uniform."""

    kind: Literal["paragraph-length-variance"] = "paragraph-length-variance"
    spread_floor: float = Field(
        default=12.0,
        description="Standard deviation of paragraph word counts below which "
        "the paragraphing reads as templated and the row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        lengths = [paragraph.length for paragraph in draft.prose.paragraphs]
        measured = spread(lengths)
        return self.result(
            f"spread {measured:.1f} words across {len(lengths)} paragraph(s), "
            f"floor {self.spread_floor}",
            fired=len(lengths) > 1 and measured < self.spread_floor,
        )


INFLATED_COPULAS = [
    "serves as",
    "stands as",
    "holds the distinction of being",
    "features",
    "boasts",
    "offers",
    "represents",
    "constitutes",
    "amounts to",
]
"""The substitutions the author's guidance names for a plain `is` — a copula
wearing more words. The default for `LinkingPredicates`, which takes an
override: which phrasings read as inflated is a judgement about register."""


class LinkingPredicates(FormatCheck):
    """How much of the draft describes with an inflated copula rather than acts.

    A declared detector: the author enumerated the substitutions that stand in
    for `is` — `serves as`, `stands as`, `holds the distinction of being` — and
    this matches them on each sentence's own words, so extending the list is
    data. A `Segmenter` backed by a parser would let the same row generalize to
    constructions nobody wrote down, and the row above would not change.
    """

    kind: Literal["linking-predicates"] = "linking-predicates"
    phrases: list[str] = Field(
        default_factory=INFLATED_COPULAS.copy,
        description="The inflated copula substitutions this row counts",
    )
    share_ceiling: float = Field(
        default=0.2,
        description="Share of sentences that may predicate with one before the "
        "row fires",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        sentences = draft.prose.sentences
        linking = [
            sentence
            for sentence in sentences
            if any(
                contains(sentence.words, draft.phrase(phrase))
                for phrase in self.phrases
            )
        ]
        measured = share(len(linking), len(sentences))
        return self.result(
            f"{len(linking)}/{len(sentences)} sentence(s) predicate with an "
            f"inflated copula ({measured:.0%}, ceiling {self.share_ceiling:.0%})",
            fired=measured > self.share_ceiling,
            findings=[sentence.text for sentence in linking],
        )


PARTICIPIAL_GERUNDS = [
    "highlighting",
    "underscoring",
    "demonstrating",
]
"""The gerunds the author's guidance names as the tell. The default set for
`ParticipialTails`, which a format extends rather than restates."""


class ParticipialTails(FormatCheck):
    """Sentences that trail off into a comma and a gerund.

    The construction adds a clause that asserts nothing, and it appears in
    machine prose far more than in anybody's writing. Detected by position on
    the sentence's own tokens — a comma, the gerund straight after it, and the
    sentence ending within reach — rather than by walking characters.
    """

    kind: Literal["participial-tails"] = "participial-tails"
    gerunds: list[str] = Field(
        default_factory=PARTICIPIAL_GERUNDS.copy,
        description="The gerunds a trailing clause may not open on",
    )
    extra_gerunds: list[str] = Field(
        default_factory=list,
        description="Further gerunds this format adds, so extending the set "
        "does not mean restating it",
    )
    tail_window: int = Field(
        default=8,
        description="How many tokens from the end the comma must fall within "
        "for the clause to count as a tail rather than a mid-sentence aside",
    )
    share_ceiling: float = Field(
        default=0.1,
        description="Share of sentences that may end on one before the row fires",
    )

    def tail(self, sentence: Sentence) -> str:
        """The trailing comma-plus-gerund clause, or empty if there is none."""
        watched = {*self.gerunds, *self.extra_gerunds}
        tokens = sentence.tokens
        opens = [
            index
            for index, token in enumerate(tokens[:-1])
            if token == ","
            and tokens[index + 1] in watched
            and len(tokens) - index <= self.tail_window
        ]
        return " ".join(tokens[opens[0] + 1 :]) if opens else ""

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        sentences = draft.prose.sentences
        tailed = [sentence for sentence in sentences if self.tail(sentence)]
        measured = share(len(tailed), len(sentences))
        return self.result(
            f"{len(tailed)}/{len(sentences)} sentence(s) end on a participial "
            f"tail ({measured:.0%}, ceiling {self.share_ceiling:.0%})",
            fired=measured > self.share_ceiling,
            findings=[sentence.text for sentence in tailed],
        )


class BoldedSummaries(FormatCheck):
    """How many paragraphs open on a bolded sentence summarizing their claim.

    A band rather than a floor, because the convention cuts both ways: a
    format built so a reader can follow the bold alone declares a floor of
    every paragraph, and a format where uniform bolding reads as a template
    declares a ceiling instead.
    """

    kind: Literal["bolded-summaries"] = "bolded-summaries"
    share_floor: float = Field(
        default=0.0,
        description="Share of paragraphs that must open on a bolded sentence",
    )
    share_ceiling: float = Field(
        default=1.0,
        description="Share of paragraphs that may open on a bolded sentence",
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        paragraphs = draft.prose.paragraphs
        bolded = [p for p in paragraphs if p.summary_is_bolded]
        measured = share(len(bolded), len(paragraphs))
        below = measured < self.share_floor
        above = measured > self.share_ceiling
        missing = [p for p in paragraphs if not p.summary_is_bolded]
        offenders = missing if below else bolded
        return self.result(
            f"{len(bolded)}/{len(paragraphs)} paragraph(s) open on a bolded "
            f"summary ({measured:.0%}, band {self.share_floor:.0%}–"
            f"{self.share_ceiling:.0%})",
            fired=bool(paragraphs) and (below or above),
            findings=[p.block.plain for p in offenders],
        )


class ParagraphSentences(FormatCheck):
    """Paragraphs longer or shorter than the format reads well at."""

    kind: Literal["paragraph-sentences"] = "paragraph-sentences"
    floor: int = Field(default=1, description="Fewest sentences a paragraph may run")
    ceiling: int = Field(default=6, description="Most sentences a paragraph may run")

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        outside = [
            f"{len(p.sentences)} sentence(s): {p.block.plain}"
            for p in draft.prose.paragraphs
            if not self.floor <= len(p.sentences) <= self.ceiling
        ]
        return self.result(
            f"{len(outside)}/{len(draft.prose.paragraphs)} paragraph(s) outside "
            f"{self.floor}–{self.ceiling} sentences",
            fired=bool(outside),
            findings=outside,
        )


class BlockLength(FormatCheck):
    """Blocks past the length the format's surface allows."""

    kind: Literal["block-length"] = "block-length"
    character_ceiling: int = Field(
        default=260, description="Characters a single block may run to"
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        over = [
            f"{len(block.plain)} chars: {block.plain}"
            for block in draft.prose.blocks
            if not block.is_heading and len(block.plain) > self.character_ceiling
        ]
        return self.result(
            f"{len(over)} block(s) over {self.character_ceiling} characters",
            fired=bool(over),
            findings=over,
        )


class DraftWords(FormatCheck):
    """Whether the whole draft lands in the length the format targets."""

    kind: Literal["draft-words"] = "draft-words"
    floor: int | None = Field(
        default=None, description="Fewest words the format targets, if it states one"
    )
    ceiling: int | None = Field(
        default=None, description="Most words the format targets, if it states one"
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        words = draft.prose.length
        short = self.floor is not None and words < self.floor
        long = self.ceiling is not None and words > self.ceiling
        return self.result(
            f"{words} words, target {self.floor or '—'}–{self.ceiling or '—'}",
            fired=short or long,
            findings=[f"{words} words, {'under' if short else 'over'} target"]
            if short or long
            else [],
        )


class SectionLength(FormatCheck):
    """How far a reader goes without a heading."""

    kind: Literal["section-length"] = "section-length"
    word_ceiling: int = Field(
        default=800, description="Words a section may run before it wants splitting"
    )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        over = [
            f"{section.length} words under “{section.heading or 'the opening'}”"
            for section in draft.prose.sections
            if section.length > self.word_ceiling
        ]
        longest = max((section.length for section in draft.prose.sections), default=0)
        return self.result(
            f"longest section {longest} words, ceiling {self.word_ceiling}",
            fired=bool(over),
            findings=over,
        )


class JudgedRow(FormatCheck):
    """A row that spends a reviewer on what counting cannot decide.

    Declared beside the mechanical rows and reported in the same shape: the
    reviewer's verdict lands in a `CheckRow`, so the rewrite stage reads one
    report and cannot tell which tier a line came from.
    """

    kind: Literal["judged"] = "judged"
    question: str = Field(
        description="What the reviewer must decide, stated so it can be "
        "answered against the draft alone"
    )

    @property
    def mechanical(self) -> bool:
        """A judged row spends a reviewer, which is the whole of what it is."""
        return False

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        verdict = await draft.decide(self.question)
        return self.result(
            verdict.measured, fired=not verdict.holds, findings=verdict.findings
        )


DeclaredCheck = Annotated[
    BannedVocabulary
    | PunctuationDensity
    | MarkdownArtifacts
    | BoldEmphasis
    | FormulaicOpenings
    | SentenceLengthVariance
    | ParagraphLengthVariance
    | LinkingPredicates
    | ParticipialTails
    | BoldedSummaries
    | ParagraphSentences
    | BlockLength
    | DraftWords
    | SectionLength
    | JudgedRow,
    Field(discriminator="kind"),
]
"""Every kind of row a format may declare, for reading one back from outside
Python — a custom format's rows arrive over a tool as JSON and validate
against exactly the declarations written here.

This union deserializes; it never dispatches. What a row *does* stays on the
row, so a new kind is one class above and no arm anywhere."""


def unplaced(markdown: str, record: BookRecord) -> list[UnresolvedReference]:
    """Every reference in one passage of markdown this book could not place.

    Rendered the way the delivered document is rendered, so that what decides
    a destination here is the same `BookReferences.destination` the write goes
    through rather than a second reading of the book free to disagree with it.
    The requests it builds are thrown away: what is wanted is what the book
    could not answer while it built them.

    A passage at a time, because a finding has to name the page the reference
    sits on and the words it sits in, and the walk over a whole document
    reports neither. The cost is a link written in markdown's reference style,
    whose destination is defined in a block of its own: that definition belongs
    to the document rather than to any passage, so it is invisible here and
    such a reference goes unreported. Every reference this codebase asks a
    writer for is inline — :func:`~inkwell.agent.book_links.pointing` spells
    the form — and no passage-level walk can see past its own passage.
    """
    references = BookReferences(record=record)
    markdown_to_requests(markdown, references=references)
    return references.unresolved


def listed(findings: Sequence[str], ceiling: int) -> list[str]:
    """The findings a row prints, saying so where it printed only some.

    A list cut short with nothing said reads as everything it left out having
    resolved, which is the one thing a report about broken links must never
    imply.
    """
    if len(findings) <= ceiling:
        return list(findings)
    return [
        *findings[:ceiling],
        f"… and {len(findings) - ceiling} more not listed, "
        f"{len(findings)} broken reference(s) in all",
    ]


class PointedReference(BaseModel, frozen=True):
    """One reference a draft made that its book could not place, and where it sits.

    The address is the passage's rather than the target's, because it is the
    one a reader meets the broken link on and the one whoever publishes has to
    open to see it.
    """

    source: BookAddress = Field(
        description="The published page the passage carrying the reference is on"
    )
    reference: UnresolvedReference = Field(
        description="What the reference named, and the words it was written on"
    )
    passage: str = Field(description="The passage it sits in, as a reader meets it")


class UnresolvedReferences(FormatCheck):
    """References that would ship as broken links into this book's own pages.

    A chapter and its sections are addressed as public URL paths, so a
    reference the book cannot place is a link a reader follows to nothing
    rather than a cosmetic gap. What the row reports is therefore a fact about
    published addresses and not a judgement about the writing, which is also
    why it is declared per run rather than by a format: a reference can only go
    unresolved where there is a book to resolve it against, and the formats
    that can carry no chapters would each be declaring a permanently silent
    row. It is no member of :data:`DeclaredCheck` for the same reason — it is
    built from a book's record, which is not something a format description
    could state nor a run could be asked to supply.

    **What is not a finding.** A book still being written is full of references
    that do not resolve and should not: chapter three points at chapter seven
    before anything has numbered it, which is exactly what
    :func:`~inkwell.agent.book_links.pointing` asks a writer to do. The line is
    drawn by the book's own record rather than by a threshold nobody wrote
    down — only a reference whose target chapter has been written was ever
    expected to resolve.

    **Both scopes.** This chapter's prose is resolved through the same
    resolver that renders the deliverable. Beside it the book's own retirements
    are read, so a chapter that places every reference it makes still surfaces
    what the book as a whole does not — a page it stopped serving while the
    chapter behind it goes on existing, which breaks references written in
    chapters this run cannot see.
    """

    kind: Literal["unresolved-references"] = "unresolved-references"
    name: str = "book references"
    rule: str = (
        "Every reference into this book points at a chapter by its declared "
        "key, and one whose target chapter has already been written resolves "
        "to a published path. What this row reports is a link fact rather than "
        "a judgement about the writing — whether /chapters/03/07 is a page "
        "this book serves is not something a voice settles — and it is "
        "advisory like every row here: it says what would ship as a broken "
        "link and stops nothing. Pointing forward at a chapter nobody has "
        "written yet is not a fault and is not reported."
    )
    record: BookRecord = Field(
        description="The book this chapter is written into, as its record stands"
    )
    placement: ChapterPlacement = Field(
        description="Which chapter of that book this draft is, which is what "
        "the published address of a finding is measured from"
    )
    listed_ceiling: int = Field(
        default=20,
        description="How many broken references are listed before the row "
        "states only how many more there are",
    )

    def source(self, heading: str) -> BookAddress:
        """Where a passage under ``heading`` is published, at the depth it sits.

        A section this chapter's own record gives an ordinal is addressed as
        that section; anything else is addressed as the chapter, because a
        section holding no ordinal has no page of its own. Placed through
        :func:`~inkwell.agent.book.placed_sections`, which is the same rule
        that decides where a reference *into* this chapter lands.
        """
        chapter = ChapterAddress(chapter=self.placement.chapter)
        held = self.record.chapter(self.placement.chapter)
        if held is None or not heading:
            return chapter
        for placed in placed_sections(held.sections):
            if placed.section is not None and placed.answers(heading):
                return SectionAddress(
                    chapter=self.placement.chapter, section=placed.section
                )
        return chapter

    def pointed(self, draft: DraftUnderCheck) -> Iterator[PointedReference]:
        """Every reference this draft makes that the book could not place.

        Each paired with the published address of the passage carrying it,
        which is what a heading changes as the walk passes one: the block a
        reference sits in decides which page a reader meets it on.
        """
        source = self.source("")
        for block in draft.prose.blocks:
            if block.is_heading:
                source = self.source(block.text)
            for reference in unplaced(block.source, self.record):
                yield PointedReference(
                    source=source, reference=reference, passage=block.plain
                )

    def here(self, pointed: Sequence[PointedReference]) -> Iterator[str]:
        """Each of this chapter's references that a written chapter did not place.

        Two ways a written target fails to place, and the finding says which,
        because they are fixed differently: a chapter still in the reading
        order that records no such section wants the section named or written,
        and a chapter that has left the reading order wants the reference
        pointed somewhere a reader can still reach.
        """
        for pointing in pointed:
            target = pointing.reference.target
            written = self.record.written(target.chapter)
            if written is None:
                continue
            missing = (
                f"records no section “{target.section}”"
                if self.record.keyed(target.chapter) is not None
                else "is no longer in the reading order"
            )
            yield (
                f"{pointing.source.path} — “{pointing.reference.text}” points "
                f"at {LINK_SCHEME}:{target.spelled()}, and {target.chapter} is "
                f"written at "
                f"{ChapterAddress(chapter=written.placement.chapter).path} but "
                f"{missing}. In: {pointing.passage}"
            )

    def across(self) -> Iterator[str]:
        """Each chapter this book wrote that the reading order no longer holds.

        The book's own scope, and the one this chapter's prose cannot show: a
        chapter dropped from the order keeps its ordinal and keeps its record
        on file, so its page stops being served while its content goes on
        existing, and every reference to it breaks at once — in this chapter,
        and in chapters delivered long before anyone dropped it.

        The retirement is read rather than the references into it, because the
        retirement is what survives. :meth:`~inkwell.agent.book.BookOutline.
        relaid` rebuilds ``cross_references`` from the layout it is handed and
        carries only those with both ends in the new reading order, so the very
        relayout that retires a chapter discards the references naming it —
        into a log, and nowhere a later run could read. What is left on file is
        the retired entry beside the chapter record it still has, which is the
        same breakage in the form the book actually keeps.
        """
        outline = self.record.outline
        if outline is None:
            return
        for entry in outline.retired:
            if self.record.chapter(entry.ordinal) is None:
                continue
            yield (
                f"{ChapterAddress(chapter=entry.ordinal).path} — “{entry.title}” "
                f"is written and the reading order no longer holds it, so every "
                f"reference to {LINK_SCHEME}:{entry.key} breaks wherever it was "
                f"written, in this chapter or in one already delivered"
            )

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        try:
            pointed = list(self.pointed(draft))
            here = list(self.here(pointed))
            across = list(self.across())
        except Exception:
            logger.exception("Book references could not be read off this draft")
            return self.result(
                "this book's references could not be read — nothing reported",
                fired=False,
            )
        broken = [*here, *across]
        return self.result(
            f"this chapter: {len(here)} reference(s) into a written chapter "
            f"did not resolve, {len(pointed) - len(here)} forward reference(s) "
            f"not expected to yet; this book: {len(across)} written chapter(s) "
            f"the reading order no longer holds",
            fired=bool(broken),
            findings=listed(broken, self.listed_ceiling),
        )


class DraftCheckReport(BaseModel):
    """Every declared row's result for one draft.

    Advisory throughout, and shaped so it cannot be anything else: the report
    carries rows and nothing a caller could read as a refusal.
    """

    format_key: str = Field(description="The format whose rows these are")
    rows: list[CheckRow] = Field(
        default_factory=list,
        description="One row per declared check, in declared order",
    )

    @property
    def fired(self) -> list[CheckRow]:
        """The rows that fired, for a summary line."""
        return [row for row in self.rows if row.fired]

    def render(self) -> str:
        """The report as `dev check` prints one, under the precedence note."""

        def lines() -> Iterator[str]:
            """The heading, the precedence, then a named row per check."""
            yield f"# Format checks: {self.format_key}"
            yield ""
            yield ADVISORY_PREAMBLE
            yield ""
            if not self.rows:
                yield "no rows declared for this format"
                return
            for row in self.rows:
                yield from row.render()
            yield ""
            yield f"{len(self.fired)}/{len(self.rows)} rows fired (advisory)"

        return "\n".join(lines())


async def run_format_checks(
    content: str,
    checks: Sequence[FormatCheck],
    *,
    format_key: str,
    draft_path: Path,
    judge: Judge,
    reader: ProseReader,
) -> DraftCheckReport:
    """Every declared row's verdict on one draft.

    Advisory by construction rather than by convention: a fired row becomes a
    line in the returned report, and there is no return value, exception, or
    flag by which a row can refuse a draft. No stage can come to depend on one.
    """
    draft = DraftUnderCheck(
        prose=reader.read(content),
        draft_path=draft_path,
        judge=judge,
        reader=reader,
    )
    rows = await asyncio.gather(*(check.run(draft) for check in checks))
    return DraftCheckReport(format_key=format_key, rows=list(rows))


def render_declared_rules(checks: Sequence[FormatCheck]) -> str:
    """The declared rows as the rules a writer reads before drafting.

    Rendered from the same declarations the check re-reads the draft against,
    so what the writer is asked for and what the draft is measured by are one
    thing stated once.
    """
    if not checks:
        return ""

    def lines() -> Iterator[str]:
        """The heading, then the rule each row measures."""
        yield "### Rules this format checks"
        yield ""
        yield (
            "Every rule below is re-read off the finished draft and reported "
            "back. They are advisory, and the author's voice outranks them."
        )
        yield ""
        yield from (f"- **{check.name}** — {check.rule}" for check in checks)

    return "\n".join(lines())


JUDGE_SYSTEM = """\
You judge one question about one draft — the question a count cannot answer. \
Read the draft, answer only what is asked, and name the specific passages that \
fall short rather than describing the problem in general.

You are advisory. The author's voice outranks the convention you are checking: \
if a passage falls short of the rule but is the author writing as they write, \
say it holds. Do not restate the rule back as a finding, and do not invent \
findings to look thorough — a draft that meets the question holds, with no \
findings at all."""


class QueryJudge(Judge):
    """Spends a reviewer on a judged row, through the pipeline's own query path."""

    def __init__(self, stage: PipelineStage = "review") -> None:
        self.stage: PipelineStage = stage

    async def rule(self, question: str, draft_path: Path) -> Verdict:
        """The reviewer's verdict, or a held verdict when none came back."""
        from inkwell.agent.client import query

        result = await query(
            f"Answer this about the draft:\n\n{question}\n\n"
            f"Read the draft from: {draft_path}",
            output_type=Verdict,
            model=stage_model(self.stage),
            tools=["Read"],
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
            system_prompt=JUDGE_SYSTEM,
        )
        return result or Verdict(
            holds=True, measured="no verdict returned by the reviewer"
        )
