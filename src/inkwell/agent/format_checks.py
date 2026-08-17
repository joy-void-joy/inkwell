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
"""

import asyncio
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from inkwell.agent.config import PipelineStage, stage_model
from inkwell.agent.glossary import GlossaryScope, GlossaryView
from inkwell.agent.prose import Paragraph, Prose, ProseReader, Sentence, spread

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

    Composes the segmenter, the reviewer, and the shared glossary so a row
    reads parsed prose, asks for a verdict, and asks what the book has already
    named without holding any of those engines itself.
    """

    def __init__(
        self,
        prose: Prose,
        draft_path: Path,
        judge: Judge,
        reader: ProseReader,
        glossary: GlossaryScope | None = None,
    ) -> None:
        self.prose = prose
        self.draft_path = draft_path
        self.judge = judge
        self.reader = reader
        self.glossary = glossary

    def phrase(self, text: str) -> list[str]:
        """A declared phrase as the words it is matched by.

        Tokenized by the same segmenter that read the draft, so `crucial`
        matches the word and not the inside of `crucially`.
        """
        return self.reader.phrase(text)

    async def decide(self, question: str) -> Verdict:
        """A reviewer's verdict on something counting cannot decide."""
        return await self.judge.rule(question, self.draft_path)

    def canon(self) -> GlossaryView | None:
        """What this run's book has already named, and nothing where it has none.

        Read through the accessor the glossary tools read through, so what a
        row measures a draft against is the same reading `lookup_terms` hands
        the writer rather than a second reading of that file layout. A run
        belonging to no book answers None, which a row reports as having
        measured nothing.
        """
        return self.glossary.canon() if self.glossary else None


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


class TerminologyDrift(FormatCheck):
    """A name the book has settled, appearing in this draft under a rival one.

    The book glossary is what makes this knowable at all: a term chapter one
    defined is on file when chapter nine runs, months later and in another
    process. First-definition-wins keeps two chapters from defining one term
    twice, and cannot keep a later chapter from calling that thing something
    else — least of all a chapter that never called `define_term` and so never
    met the mechanism.

    Mechanical, because what it matches is written down: the rival names each
    entry recorded as ones it rejected. A synonym nobody wrote down is
    invisible here, which is why the rule this row declares says so — a quiet
    row means no *recorded* rival name was used, not that the draft is
    terminologically consistent. Reading the canon off the draft rather than
    off a field of this class is what leaves that gap to a judged sibling: a
    row that asks a reviewer which passages rename a concept in words the
    glossary never anticipated is another class here, reading the same
    `canon`, and no change to this one.
    """

    kind: Literal["terminology-drift"] = "terminology-drift"

    async def run(self, draft: DraftUnderCheck) -> CheckRow:
        canon = draft.canon()
        if canon is None:
            return self.result(
                "no book glossary — this piece belongs to no book, so nothing "
                "was measured",
                fired=False,
            )
        renamed = [
            f"“{alias}” — {sentence.text} — this book calls it “{entry.term}”"
            for sentence in draft.prose.sentences
            for entry in canon.terms
            for alias in entry.rejected()
            if contains(sentence.words, draft.phrase(alias))
        ]
        rivals = sum(len(entry.rejected()) for entry in canon.terms)
        return self.result(
            f"{len(renamed)} passage(s) name something this book settled "
            f"otherwise, across {len(canon.terms)} term(s) on file and the "
            f"{rivals} rival name(s) they record",
            fired=bool(renamed),
            findings=renamed,
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
    | TerminologyDrift
    | JudgedRow,
    Field(discriminator="kind"),
]
"""Every kind of row a format may declare, for reading one back from outside
Python — a custom format's rows arrive over a tool as JSON and validate
against exactly the declarations written here.

This union deserializes; it never dispatches. What a row *does* stays on the
row, so a new kind is one class above and no arm anywhere."""


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
    glossary: GlossaryScope | None = None,
) -> DraftCheckReport:
    """Every declared row's verdict on one draft.

    Advisory by construction rather than by convention: a fired row becomes a
    line in the returned report, and there is no return value, exception, or
    flag by which a row can refuse a draft. No stage can come to depend on one.

    A caller with no glossary to hand leaves it out, and the rows that measure
    a draft against what the piece has named report having measured nothing.
    """
    draft = DraftUnderCheck(
        prose=reader.read(content),
        draft_path=draft_path,
        judge=judge,
        reader=reader,
        glossary=glossary,
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
