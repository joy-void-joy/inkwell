"""Reader feedback on already-published text, ingested one file per address.

The live channels — Google Doc comments, terminal input, tab edits — carry
only the author, and only while a run is in flight. Readers of text that
already shipped are a second channel with its own shape: an export from
whatever form collected them, read once at session start and filed under the
ordinal path each row names.

Both sides of that filing use the same identity. The export keys its rows by
chapter and section number; ``SectionAddresses`` reads the same path off the
plan's own sections and off the chapter the plan says it is. A row naming a
chapter and a section addresses that section; a row naming a chapter alone is
about the chapter whole and is filed under the chapter. So a stage revising
one section opens one file, a stage working on the chapter opens its chapter's
file, and no theme drawn from any particular round of feedback is carried in a
prompt — the stage reads the evidence.

The whole export is filed, whatever chapter each row names, and a run selects
its own chapter when it reads. One ingestion therefore serves every chapter
run of a book, a chapter run never clears a sibling chapter's files, and a
correction to the export replaces the set rather than layering on it.

The export this was built against is a Formspree JSON dump: a metadata
header, then one row per submission — most a bare up/down vote, a minority
carrying prose.

    notes/reader/
    ├── sections/01.03.md   # one file per addressed section
    ├── chapters/01.md      # one file per chapter written about whole
    ├── unrouted.md         # submissions no ordinal addressed at all
    ├── index.md            # the filed set, with a path per address
    └── ingestion.json      # what the run read, kept, and could not route
"""

import json
import logging
from collections.abc import Iterator, Sequence
from itertools import groupby
from pathlib import Path
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
)

from lup.types import JsonObject, JsonValue

from inkwell.agent.book import ChapterPlacement
from inkwell.agent.models import ArticlePlan

logger = logging.getLogger(__name__)

EXPORT_SUFFIXES: tuple[str, ...] = (".json",)
"""Default suffixes a configured reader-feedback directory is scanned for.

Which shapes this reader can parse is a judgement about this reader, not a
property of reader feedback, so it is a default every entry point takes as an
argument — a form that also exports CSV needs a caller's override, not a fork.
Whatever a scan passes over is counted and named, so a dropped file that this
reader cannot read says so instead of reading as an empty inbox."""

ORDINAL_CHARS = "0123456789.:-—"
"""Digits and the separators an outline number is written with. A token
carrying anything else is a word, so the title opens with prose rather than
with an ordinal."""

UNADDRESSED = "no chapter and no section ordinal — an incomplete row"
"""Why a substantive submission ends up unrouted rather than on an address.

A row naming a chapter is about that chapter even where it names no section,
so it is filed under the chapter rather than landing here. Only a row naming
neither has nothing to be filed under."""


class ProseField(BaseModel):
    """One prose answer a submission carries, under the form field that asked."""

    field: str = Field(description="Form field the reader wrote this under")
    text: str = Field(description="What they wrote, verbatim")


class SubstantiveRule(BaseModel):
    """What separates a substantive submission from a bare up/down vote.

    Which fields have to carry prose, and how much of it, is a judgement about
    a particular form rather than a property of reader feedback, so both are
    defaults a caller overrides rather than constants they would have to fork.
    """

    prose_fields: list[str] = Field(
        default_factory=lambda: ["comments", "improvement", "has_detailed_feedback"],
        description=(
            "Form fields whose prose makes a submission substantive. "
            "``has_detailed_feedback`` is here because the form that produced "
            "the first export sometimes lands the detailed feedback itself in "
            "the flag field meant to announce it."
        ),
    )
    min_length: int = Field(
        default=12,
        description=(
            "Shortest prose that still says something. Below this a field is a "
            "stray keystroke or a bare 'good', not feedback to act on."
        ),
    )

    def substantive_prose(self, submission: "ReaderSubmission") -> list[ProseField]:
        """The prose this rule counts, in the order the form asked for it."""
        return [
            prose
            for prose in submission.prose()
            if prose.field in self.prose_fields
            and len(prose.text.strip()) >= self.min_length
        ]


class SectionAddress(BaseModel):
    """Where a submission or a plan section sits in the work's ordinal outline.

    The one identity both sides share: the export keys rows by chapter and
    section number, and the plan's own sections yield the same path.
    """

    model_config = ConfigDict(frozen=True)

    chapter: int = Field(description="1-based chapter ordinal")
    section: int = Field(description="1-based section ordinal within the chapter")

    @property
    def key(self) -> str:
        """The address as a file stem, zero-padded so a listing sorts."""
        return f"{self.chapter:02d}.{self.section:02d}"

    def label(self) -> str:
        """The address as a reader of the outline would say it."""
        return f"{self.chapter}.{self.section}"


def ordinal_prefix(title: str) -> SectionAddress | None:
    """The ordinal path a section title opens with, where it opens with one.

    A title carried over from an outline usually keeps its number — "1.3
    Foundation models", "01.03 Foundation models" — and that number is the
    section's own identity, outranking its position in the plan, which shifts
    whenever a section is added or dropped. Read as the digit runs of the
    opening token: exactly two of them is a chapter and a section, and any
    other shape belongs to the prose.
    """
    opening = title.split()
    if not opening:
        return None
    token = opening[0]
    if any(char not in ORDINAL_CHARS for char in token):
        return None
    runs = ["".join(chars) for digit, chars in groupby(token, str.isdigit) if digit]
    if len(runs) != 2:
        return None
    return SectionAddress(chapter=int(runs[0]), section=int(runs[1]))


class PlanSectionAddress(BaseModel):
    """Where a plan section sits in the outline, as far as the plan says.

    The section ordinal is always known; the chapter is absent only when the
    plan is genuinely unplaced — no chapter placement of its own, and no title
    carrying an ordinal to read one off. A plan that says which chapter of
    which book it is places every section it places, whatever its titles are
    spelled like.
    """

    title: str = Field(description="The plan's own title for the section")
    section: int = Field(description="1-based section ordinal")
    chapter: int | None = Field(
        default=None, description="Chapter ordinal, where the plan names one"
    )


class AddressedFile(BaseModel):
    """One section's feedback file, and the ordinal path it is filed under."""

    address: SectionAddress
    path: Path


class ChapterFile(BaseModel):
    """One chapter's own feedback file, and the chapter it is filed under."""

    chapter: int = Field(description="1-based chapter ordinal")
    path: Path


class SectionAddresses(BaseModel):
    """Where each plan section sits in the outline the export is keyed by.

    Built once from the plan and handed to whatever needs it, so the mapping
    between the export's keys and the plan's sections is read from one
    declaration rather than re-derived wherever a section is looked up.

    A section the plan does not place has no entry at all. That is the point
    of the type: an entry is an identity, and there is no way to write down a
    position and have it read as one.
    """

    entries: list[PlanSectionAddress] = Field(default_factory=list)

    @classmethod
    def for_plan(cls, plan: ArticlePlan) -> Self:
        """Place every section of ``plan`` the plan itself places.

        The chapter comes from the plan's own placement, which is the one
        thing that actually knows it: a plan launched as chapter 7 of a book
        places its sections in chapter 7 whether or not any title says so, and
        outranks a title's own prefix where the two disagree, because the
        placement is a recorded decision and a carried-over prefix is not.
        Where the plan is unplaced, a title's ordinal supplies the chapter as
        it always did, and where no title carries one either, the chapter
        stays unknown rather than being guessed at.

        The section ordinal is the title's own where it has one, and its
        position in the plan otherwise.

        A mixed plan — numbered sections beside an unnumbered introduction or
        conclusion — places only the numbered ones. Position inside a chapter
        whose other sections have named themselves is not an identity: the
        introduction of such a plan sits before section 1, not at it, so
        reading its position as an ordinal would hand that writer section
        1.1's readers. Unplaced is the honest answer, and the stage still has
        the index and the unrouted file.
        """
        placed = plan.placement.chapter if plan.placement is not None else None
        titled = [ordinal_prefix(section.title) for section in plan.sections]
        if any(own is not None for own in titled):
            return cls(
                entries=[
                    PlanSectionAddress(
                        title=section.title,
                        section=own.section,
                        chapter=placed if placed is not None else own.chapter,
                    )
                    for section, own in zip(plan.sections, titled)
                    if own is not None
                ]
            )
        return cls(
            entries=[
                PlanSectionAddress(
                    title=section.title, section=position, chapter=placed
                )
                for position, section in enumerate(plan.sections, 1)
            ]
        )

    def entry_for(self, title: str) -> PlanSectionAddress | None:
        """Where the plan places one of its sections, by the plan's own title."""
        return next((entry for entry in self.entries if entry.title == title), None)

    @property
    def chapter(self) -> int | None:
        """Which chapter this plan is, where every section it places agrees.

        A plan that says which chapter of which book it is places all of its
        sections there, so the answer is that chapter. An unplaced plan whose
        titles carry one chapter's ordinals is that chapter too: the titles
        are the only thing that said so, and they agree.

        A plan whose sections span chapters is not one chapter, and neither is
        a plan that places none — both answer nothing rather than picking one,
        because what this is read for is which chapter's readers a run may
        reach, and a wrong answer reaches somebody else's.
        """
        named = {entry.chapter for entry in self.entries if entry.chapter is not None}
        return next(iter(named)) if len(named) == 1 else None


class ReaderSubmission(BaseModel):
    """One reader's submission, as the export's form actually records it.

    Unknown fields are ignored rather than refused: a form gains questions
    over time, and rejecting a whole row over a field this pipeline has no
    use for would throw away the prose the row was submitted to carry.
    """

    model_config = ConfigDict(extra="ignore")

    feedback_type: str = Field(
        default="",
        description=(
            "Which form produced the row: section, section_targeted, chapter_conclusion"
        ),
    )
    vote: str | None = Field(
        default=None, description="'up' or 'down' on the bare-vote form"
    )
    chapter_number: int | None = Field(default=None, description="Chapter ordinal")
    section_number: int | None = Field(default=None, description="Section ordinal")

    comments: str | None = Field(default=None, description="Free-text feedback")
    improvement: str | None = Field(default=None, description="What to change")
    has_detailed_feedback: bool | str | None = Field(
        default=None,
        description=(
            "The form's flag for whether prose follows — which sometimes "
            "carries the prose itself instead of a boolean"
        ),
    )

    overall_rating: int | None = Field(default=None, description="Rating out of 10")
    understanding: int | None = Field(default=None, description="Comprehension score")
    writing_clarity: int | None = Field(default=None, description="Clarity score")
    conceptual_coherence: int | None = Field(
        default=None, description="Coherence score"
    )
    multimedia_desire: int | None = Field(
        default=None, description="Appetite for media"
    )
    reading_length: int | None = Field(default=None, description="Length preference")

    page_type: str | None = Field(default=None, description="'section' or 'chapter'")
    page_title: str | None = Field(default=None, description="Heading the reader saw")
    pathname: str | None = Field(default=None, description="URL path of the page read")
    content_hash: str | None = Field(
        default=None, description="Hash of the text the reader actually read"
    )
    version: str | None = Field(default=None, description="Content version tag")
    content_version: str | None = Field(
        default=None, description="Content version on the detailed form"
    )

    contact_name: str | None = Field(default=None, description="Name, when offered")
    contact_email: str | None = Field(default=None, description="Email, when offered")
    contact_organization: str | None = Field(
        default=None, description="Organization, when offered"
    )

    submitted_at: str | None = Field(default=None, description="Client-side timestamp")
    submission_date: str | None = Field(default=None, description="Submission date")
    submission_timestamp: str | None = Field(
        default=None, description="Detailed-form timestamp"
    )
    user_agent: str | None = Field(default=None, description="Reader's browser")
    recorded_at: str = Field(
        default="",
        validation_alias="_date",
        description="When the form backend recorded the row",
    )

    @field_validator("chapter_number", "section_number", mode="before")
    @classmethod
    def blank_ordinal_is_absent(cls, value: JsonValue) -> JsonValue:
        """An unanswered ordinal arrives as an empty string, not as null."""
        return None if isinstance(value, str) and not value.strip() else value

    def address(self) -> SectionAddress | None:
        """The section this submission is about, where it names one.

        A chapter-level row — the conclusion form, or a section form submitted
        from a chapter index — carries no section ordinal, so it addresses no
        section. Which section of its chapter it meant is not guessed at; the
        row is filed under its chapter instead, where it keeps its identity.
        """
        if self.chapter_number is None or self.section_number is None:
            return None
        return SectionAddress(chapter=self.chapter_number, section=self.section_number)

    def prose(self) -> list[ProseField]:
        """Every prose answer the row carries, under its own field name."""
        written = [
            ProseField(field="comments", text=self.comments or ""),
            ProseField(field="improvement", text=self.improvement or ""),
            ProseField(
                field="has_detailed_feedback",
                text=self.has_detailed_feedback
                if isinstance(self.has_detailed_feedback, str)
                else "",
            ),
        ]
        return [prose for prose in written if prose.text.strip()]

    def ratings(self) -> list[str]:
        """The numeric scores the row carries, rendered for a reading eye."""
        scored = (
            ("overall", self.overall_rating),
            ("understanding", self.understanding),
            ("clarity", self.writing_clarity),
            ("coherence", self.conceptual_coherence),
            ("multimedia", self.multimedia_desire),
            ("length", self.reading_length),
        )
        return [f"{name} {score}/10" for name, score in scored if score is not None]

    def when(self) -> str:
        """The best timestamp the row carries."""
        return (
            self.submitted_at
            or self.submission_timestamp
            or self.submission_date
            or self.recorded_at
        )


class MalformedRow(BaseModel):
    """A row the export's shape did not admit, and where in the file it sat."""

    position: int = Field(description="0-based index of the row in the export")
    error: str = Field(description="What validation objected to")


class RawExport(BaseModel):
    """An export file as it arrives: its header, and rows not yet validated.

    Rows stay untyped here so one unreadable row is reported at its position
    rather than taking the whole file down with it.
    """

    model_config = ConfigDict(extra="ignore")

    email: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    host: str | None = None
    submissions: list[JsonObject] = Field(default_factory=list)


class ParsedExport(BaseModel):
    """One export file read: the rows that parsed, and the ones that did not."""

    submissions: list[ReaderSubmission] = Field(default_factory=list)
    malformed: list[MalformedRow] = Field(default_factory=list)


class ReadRow(BaseModel):
    """One row's outcome: the submission it became, or why it became none."""

    position: int = Field(description="0-based index of the row in the export")
    submission: ReaderSubmission | None = None
    error: str = Field(default="", description="What validation objected to")


def validation_message(exc: ValidationError) -> str:
    """Every objection pydantic raised, with the field each one lands on."""
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
        for detail in exc.errors(include_url=False)
    )


def read_row(position: int, row: JsonObject) -> ReadRow:
    """Validate one row, keeping its position so a failure can be named."""
    try:
        return ReadRow(
            position=position, submission=ReaderSubmission.model_validate(row)
        )
    except ValidationError as exc:
        return ReadRow(position=position, error=validation_message(exc))


def parse_export(payload: JsonValue) -> ParsedExport:
    """Read an export payload into submissions, reporting each row that fails."""
    raw = RawExport.model_validate(payload)
    rows = [read_row(position, row) for position, row in enumerate(raw.submissions)]
    return ParsedExport(
        submissions=[row.submission for row in rows if row.submission is not None],
        malformed=[
            MalformedRow(position=row.position, error=row.error)
            for row in rows
            if row.submission is None
        ],
    )


def read_export(path: Path) -> ParsedExport:
    """Read one export file from disk. A file that is not JSON is one bad row."""
    try:
        payload: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Reader feedback export %s is not readable JSON: %s", path, exc)
        return ParsedExport(malformed=[MalformedRow(position=0, error=str(exc))])
    try:
        return parse_export(payload)
    except ValidationError as exc:
        logger.warning("Reader feedback export %s has no readable header", path)
        return ParsedExport(
            malformed=[MalformedRow(position=0, error=validation_message(exc))]
        )


class ExportScan(BaseModel):
    """What a configured path holds: the files to read, and the ones passed over.

    The skipped list is why this is a model rather than a list of paths. A
    directory scan that silently dropped a file this reader cannot parse would
    report an empty inbox to an author who had just dropped their export in it.
    """

    files: list[Path] = Field(default_factory=list)
    skipped: list[Path] = Field(default_factory=list)


def scan_exports(source: Path, suffixes: Sequence[str] = EXPORT_SUFFIXES) -> ExportScan:
    """Sort what a configured path names into what this reader can read and not.

    A file named outright is read whatever it is called — the author pointed at
    it, so the suffix filter has nothing to decide. Only a directory scan
    chooses, and it says what it passed over.
    """
    if not source.is_dir():
        return ExportScan(files=[source] if source.is_file() else [])
    entries = sorted(path for path in source.iterdir() if path.is_file())
    scan = ExportScan(
        files=[path for path in entries if path.suffix in suffixes],
        skipped=[path for path in entries if path.suffix not in suffixes],
    )
    for path in scan.skipped:
        logger.warning(
            "Reader feedback: skipped %s — not one of the readable suffixes (%s)",
            path,
            ", ".join(suffixes),
        )
    return scan


class Routed(BaseModel):
    """A substantive submission, and the section address it named."""

    submission: ReaderSubmission
    address: SectionAddress


class ChapterRouted(BaseModel):
    """A substantive submission about a chapter whole, and which chapter."""

    submission: ReaderSubmission
    chapter: int = Field(description="1-based chapter ordinal the row named")


class Unroutable(BaseModel):
    """A substantive submission no ordinal addressed at all, and why not."""

    submission: ReaderSubmission
    reason: str = Field(description="What kept the submission off an address")


class SortedSubmissions(BaseModel):
    """A run's submissions, split by what ingestion can do with each."""

    routed: list[Routed] = Field(
        default_factory=list, description="Prose about an identified section"
    )
    chaptered: list[ChapterRouted] = Field(
        default=[], description="Prose about a chapter whole, naming no section"
    )
    unroutable: list[Unroutable] = Field(
        default=[], description="Prose that named no ordinal at all"
    )
    votes: int = Field(
        default=0, description="Bare votes and ratings, carrying no prose"
    )

    def by_address(self) -> dict[SectionAddress, list[ReaderSubmission]]:
        """Group section-addressed submissions under the address each named."""
        ordered = sorted(self.routed, key=lambda entry: entry.address.key)
        return {
            address: [entry.submission for entry in group]
            for address, group in groupby(ordered, key=lambda entry: entry.address)
        }

    def by_chapter(self) -> dict[int, list[ReaderSubmission]]:
        """Group chapter-level submissions under the chapter each one named."""
        ordered = sorted(self.chaptered, key=lambda entry: entry.chapter)
        return {
            chapter: [entry.submission for entry in group]
            for chapter, group in groupby(ordered, key=lambda entry: entry.chapter)
        }


def sort_submissions(
    submissions: list[ReaderSubmission], rule: SubstantiveRule
) -> SortedSubmissions:
    """Split submissions into section prose, chapter prose, loose prose, votes."""
    substantive = [one for one in submissions if rule.substantive_prose(one)]
    unaddressed = [one for one in substantive if one.address() is None]
    return SortedSubmissions(
        routed=[
            Routed(submission=one, address=address)
            for one in substantive
            if (address := one.address()) is not None
        ],
        chaptered=[
            ChapterRouted(submission=one, chapter=chapter)
            for one in unaddressed
            if (chapter := one.chapter_number) is not None
        ],
        unroutable=[
            Unroutable(submission=one, reason=UNADDRESSED)
            for one in unaddressed
            if one.chapter_number is None
        ],
        votes=len(submissions) - len(substantive),
    )


class IngestionReport(BaseModel):
    """What one ingestion run read, kept, and could not route.

    Returned so the caller can say it out loud, and written beside the files
    so a later run can tell what the numbers on disk were built from.
    """

    sources: list[str] = Field(
        default_factory=list, description="Export files this run read"
    )
    skipped: list[str] = Field(
        default_factory=list,
        description=(
            "Files in a configured directory whose suffix this reader does not "
            "parse — named so a dropped export in the wrong format is visible "
            "rather than reading as an empty inbox"
        ),
    )
    read: int = Field(default=0, description="Submissions read across every file")
    substantive: int = Field(
        default=0, description="Submissions carrying prose the rule counts"
    )
    votes: int = Field(
        default=0, description="Submissions that were a bare vote or rating"
    )
    chapter_level: int = Field(
        default=0,
        description=(
            "Substantive submissions about a chapter whole — naming a chapter "
            "but no section. Counted apart from the section-addressed ones so "
            "the substantive total still splits into the buckets it was filed "
            "into: sections, chapters, and the unrouted remainder"
        ),
    )
    unrouted: int = Field(
        default=0, description="Substantive submissions no ordinal addressed at all"
    )
    chapters: list[int] = Field(
        default=[],
        description=(
            "Which chapters got a chapter-level file, recorded for the same "
            "reason the section addresses are: a reader of the tree takes them "
            "from what the writer wrote down rather than off file names"
        ),
    )
    addresses: list[SectionAddress] = Field(
        default_factory=list,
        description=(
            "Which ordinal paths got a file, so a reader of the tree takes the "
            "addresses from what the writer recorded rather than re-reading "
            "them off file names"
        ),
    )
    malformed: list[MalformedRow] = Field(
        default_factory=list, description="Rows that failed validation, by position"
    )
    filed: bool = Field(
        default=False,
        description=(
            "Whether this run wrote the tree. False when it read no submission "
            "and so left whatever an earlier ingestion filed standing"
        ),
    )

    @computed_field
    @property
    def sections(self) -> int:
        """How many sections got a file — recorded so the report reads whole."""
        return len(self.addresses)

    def summary(self) -> str:
        """One line naming every count, for a progress message or a log."""
        return (
            f"{self.read} reader submissions read, {self.substantive} substantive "
            f"across {self.sections} section(s) and {len(self.chapters)} chapter(s), "
            f"{self.votes} bare vote(s), {self.unrouted} unroutable, "
            f"{len(self.malformed)} malformed, {len(self.skipped)} file(s) skipped"
        )


def render_submission(submission: ReaderSubmission, ordinal: int) -> str:
    """One submission as evidence: what the reader wrote, and what they read."""
    heading = f"### Submission {ordinal}"
    if when := submission.when():
        heading += f" — {when}"
    if ratings := ", ".join(submission.ratings()):
        heading += f" ({ratings})"

    def lines() -> Iterator[str]:
        """The heading, its provenance, then each answer under its question."""
        yield heading
        provenance = [
            part
            for part in (
                f"page: {submission.page_title}" if submission.page_title else "",
                f"path: {submission.pathname}" if submission.pathname else "",
                f"form: {submission.feedback_type}" if submission.feedback_type else "",
                f"content: {submission.content_hash[:12]}"
                if submission.content_hash
                else "",
            )
            if part
        ]
        if provenance:
            yield f"*{' · '.join(provenance)}*"
        for prose in submission.prose():
            yield f"**{prose.field}**"
            yield "\n".join(f"> {line}" for line in prose.text.strip().splitlines())

    return "\n\n".join(lines())


def render_section_file(address: SectionAddress, group: list[ReaderSubmission]) -> str:
    """Every submission about one section, in the order they were submitted."""
    header = (
        f"# Reader feedback on section {address.label()}\n\n"
        f"{len(group)} substantive submission(s) from readers of the published "
        f"text. Their words, not a summary — weigh them as reader evidence "
        f"about this section.\n"
    )
    body = "\n\n".join(
        render_submission(submission, ordinal)
        for ordinal, submission in enumerate(group, 1)
    )
    return f"{header}\n{body}\n"


def render_chapter_file(chapter: int, group: list[ReaderSubmission]) -> str:
    """Every submission about one chapter whole, in the order they came in."""
    header = (
        f"# Reader feedback on chapter {chapter}\n\n"
        f"{len(group)} substantive submission(s) from readers who wrote about "
        f"this chapter rather than about one of its sections. Their words, not "
        f"a summary — weigh them as reader evidence about the chapter whole: "
        f"how it opens, how it holds together, where it lost them.\n"
    )
    body = "\n\n".join(
        render_submission(submission, ordinal)
        for ordinal, submission in enumerate(group, 1)
    )
    return f"{header}\n{body}\n"


def render_unrouted_file(unroutable: list[Unroutable]) -> str:
    """Substantive submissions naming no ordinal, kept where a stage sees them."""
    header = (
        f"# Reader feedback with no address\n\n"
        f"{len(unroutable)} substantive submission(s) naming neither a chapter "
        f"nor a section. They are here rather than dropped: read them as "
        f"feedback on the work at large.\n"
    )

    def entries() -> Iterator[str]:
        """Each unroutable submission, with the reason it landed here."""
        for ordinal, entry in enumerate(unroutable, 1):
            yield f"{render_submission(entry.submission, ordinal)}\n\n*({entry.reason})*"

    return f"{header}\n" + "\n\n".join(entries()) + "\n"


def render_index_file(
    report: IngestionReport,
    routed: list[AddressedFile],
    chaptered: list[ChapterFile],
    unrouted: Path,
) -> str:
    """The filed set as a listing: one line per address, with its file path."""

    def lines() -> Iterator[str]:
        """A header carrying the counts, then the listing as one tight block."""
        yield "# Reader feedback, by address"
        yield (
            "Readers of the already-published text, filed by the ordinal path "
            "(chapter.section, or the chapter alone) their submission named. "
            "Read the file for the chapter and section you are acting on, and "
            "leave the other chapters' files to the runs that own them."
        )
        yield report.summary()
        if routed:
            yield "## Sections"
            yield "\n".join(
                f"- section {entry.address.label()}: {entry.path}" for entry in routed
            )
        if chaptered:
            yield "## Chapters"
            yield "\n".join(
                f"- chapter {entry.chapter}: {entry.path}" for entry in chaptered
            )
        if report.unrouted:
            yield "## No address"
            yield (
                f"- {report.unrouted} submission(s) about the work at large: {unrouted}"
            )

    return "\n\n".join(lines()) + "\n"


class ReaderFeedbackTree(BaseModel):
    """The layout of one run's ingested reader feedback under the notes tree."""

    root: Path = Field(description="The run's ``notes/reader`` directory")

    @property
    def sections_dir(self) -> Path:
        """Where the per-section files sit, apart from the index beside them."""
        return self.root / "sections"

    def section_path(self, address: SectionAddress) -> Path:
        """The file one ordinal address is filed under."""
        return self.sections_dir / f"{address.key}.md"

    @property
    def chapters_dir(self) -> Path:
        """Where the per-chapter files sit, beside the per-section ones."""
        return self.root / "chapters"

    def chapter_path(self, chapter: int) -> Path:
        """The file one chapter's own feedback is filed under.

        Addressed by the chapter ordinal alone, the way a section file is
        addressed by the whole path, so a run holding its own chapter reaches
        its file and has no way to name a sibling chapter's.
        """
        return self.chapters_dir / f"{chapter:02d}.md"

    @property
    def unrouted_path(self) -> Path:
        """Where feedback that named no section is kept."""
        return self.root / "unrouted.md"

    @property
    def index_path(self) -> Path:
        """The listing of the whole routed set."""
        return self.root / "index.md"

    @property
    def report_path(self) -> Path:
        """Where the counts of the last ingestion are recorded."""
        return self.root / "ingestion.json"

    def ingested(self) -> bool:
        """Whether an ingestion has written anything here."""
        return self.index_path.exists()

    def last_ingestion(self) -> IngestionReport | None:
        """What the last ingestion recorded here, where one has run."""
        if not self.report_path.exists():
            return None
        return IngestionReport.model_validate_json(
            self.report_path.read_text(encoding="utf-8")
        )

    def addressed_files(self) -> list[AddressedFile]:
        """Every per-section file the last ingestion filed, by its address."""
        report = self.last_ingestion()
        return [
            AddressedFile(address=address, path=self.section_path(address))
            for address in (report.addresses if report else [])
        ]

    def chapter_files(self) -> list[ChapterFile]:
        """Every per-chapter file the last ingestion filed, by its chapter."""
        report = self.last_ingestion()
        return [
            ChapterFile(chapter=chapter, path=self.chapter_path(chapter))
            for chapter in (report.chapters if report else [])
        ]

    def clear(self) -> None:
        """Unlink what a previous ingestion filed, leaving nothing stale behind."""
        for path in (*self.sections_dir.glob("*.md"), *self.chapters_dir.glob("*.md")):
            path.unlink()
        self.unrouted_path.unlink(missing_ok=True)

    def write(
        self, report: IngestionReport, split: SortedSubmissions
    ) -> IngestionReport:
        """File the addressed set, the remainder, the index, and the report.

        The whole export is filed here, every chapter of it, because one
        ingestion serves every chapter run of a book and a run selects its own
        chapter when it reads.

        An address with no substantive submission gets no file — an empty one
        would read to a stage as "the readers said nothing here", which is a
        different claim from "nobody wrote in about this".

        What a previous ingestion filed here is cleared first, so a corrected
        export replaces the set rather than layering on top of it: a section
        whose feedback has been withdrawn must stop having a file, not keep a
        stale one that stages go on reading.
        """
        self.clear()
        self.sections_dir.mkdir(parents=True, exist_ok=True)
        self.chapters_dir.mkdir(parents=True, exist_ok=True)
        sections = split.by_address()
        chapters = split.by_chapter()

        def filed() -> Iterator[AddressedFile]:
            """Each addressed section, written out under its ordinal path."""
            for address in sorted(sections, key=lambda seen: seen.key):
                path = self.section_path(address)
                path.write_text(
                    render_section_file(address, sections[address]), encoding="utf-8"
                )
                yield AddressedFile(address=address, path=path)

        def chaptered() -> Iterator[ChapterFile]:
            """Each chapter readers wrote about whole, under its own ordinal."""
            for chapter in sorted(chapters):
                path = self.chapter_path(chapter)
                path.write_text(
                    render_chapter_file(chapter, chapters[chapter]), encoding="utf-8"
                )
                yield ChapterFile(chapter=chapter, path=path)

        written = list(filed())
        written_chapters = list(chaptered())
        if split.unroutable:
            self.unrouted_path.write_text(
                render_unrouted_file(split.unroutable), encoding="utf-8"
            )

        report.addresses = [entry.address for entry in written]
        report.chapters = [entry.chapter for entry in written_chapters]
        report.filed = True
        self.index_path.write_text(
            render_index_file(report, written, written_chapters, self.unrouted_path),
            encoding="utf-8",
        )
        self.report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report


def ingest_reader_feedback(
    tree: ReaderFeedbackTree,
    source: Path,
    *,
    rule: SubstantiveRule | None = None,
    suffixes: Sequence[str] = EXPORT_SUFFIXES,
) -> IngestionReport:
    """Read every export ``source`` names and file it one address per file.

    Substantive submissions are routed by the ordinal path they name: a
    section where they name one, the chapter alone where they name only that,
    and the unrouted file where they name neither — counted rather than
    dropped. Bare votes are counted and left out — a stage revising prose has
    nothing to do with an up-arrow, and hundreds of them would bury the
    handful that say something.

    An export that yields no submission at all files nothing and clears
    nothing. A resume re-reads the configured path, and a truncated or corrupt
    export replacing a good ingestion with an empty one would leave every
    stage of that run with no reader evidence — so what is already on disk
    stands, and the run reports that it read nothing.
    """
    scan = scan_exports(source, suffixes)
    files = scan.files
    parsed = [read_export(path) for path in files]
    submissions = [one for export in parsed for one in export.submissions]
    malformed = [
        MalformedRow(position=row.position, error=f"{path.name}: {row.error}")
        for path, export in zip(files, parsed)
        for row in export.malformed
    ]
    for row in malformed:
        logger.warning("Reader submission at row %d: %s", row.position, row.error)

    split = sort_submissions(submissions, rule or SubstantiveRule())
    report = IngestionReport(
        sources=[str(path) for path in files],
        skipped=[str(path) for path in scan.skipped],
        read=len(submissions) + len(malformed),
        substantive=len(split.routed) + len(split.chaptered) + len(split.unroutable),
        votes=split.votes,
        chapter_level=len(split.chaptered),
        unrouted=len(split.unroutable),
        malformed=malformed,
    )
    if not submissions:
        logger.warning(
            "Reader feedback: no submission read from %s — keeping what is "
            "already filed under %s",
            source,
            tree.root,
        )
        return report
    return tree.write(report, split)


class SectionFeedbackFile(BaseModel):
    """One plan section's reader feedback, and where it sits on disk."""

    title: str = Field(description="The plan's own title for the section")
    path: Path


class ReaderFeedback(BaseModel):
    """A run's ingested reader feedback, addressed the way the plan addresses.

    Built once per stage from the notes tree and the plan in hand, so every
    stage reaches a section's file through the same declared mapping instead
    of matching titles against text.

    The tree holds the whole export, every chapter of it. Selecting this run's
    chapter is this type's job, and it is the only way in: every path handed
    out is derived from the run's own chapter, so a run has no way to name a
    sibling chapter's file and no writer is ever handed another chapter's
    readers.
    """

    tree: ReaderFeedbackTree
    addresses: SectionAddresses = Field(default_factory=SectionAddresses)
    chapter: int | None = Field(
        default=None,
        description=(
            "Which chapter of the book this run is writing, where it knows. "
            "The one thing every address handed out is selected by"
        ),
    )

    @classmethod
    def for_plan(cls, reader_dir: Path, plan: ArticlePlan | None) -> Self:
        """Read what is on disk, addressed against ``plan`` where there is one.

        The chapter is the plan's own, read off the address book rather than
        off the placement directly, so a plan that names its chapter only in
        its section titles is placed by them exactly as its sections are.
        """
        addresses = SectionAddresses.for_plan(plan) if plan else SectionAddresses()
        return cls(
            tree=ReaderFeedbackTree(root=reader_dir),
            addresses=addresses,
            chapter=addresses.chapter,
        )

    @classmethod
    def for_placement(
        cls, reader_dir: Path, placement: ChapterPlacement | None
    ) -> Self:
        """Read what is on disk before a plan exists, by the launched placement.

        The plan stage runs before any plan does. A run launched as a chapter
        of a book still knows which chapter it is, so it reaches that
        chapter's files by ordinal; a run launched without a placement knows
        nothing to select by and reads the index and the unrouted file, which
        need no mapping.
        """
        return cls(
            tree=ReaderFeedbackTree(root=reader_dir),
            chapter=placement.chapter if placement is not None else None,
        )

    def index(self) -> Path | None:
        """The listing of the whole routed set, once something has been filed."""
        return self.tree.index_path if self.tree.ingested() else None

    def unrouted(self) -> Path | None:
        """Feedback that named no ordinal at all, where any was filed.

        The one file no chapter owns, and so the one every run may read: a
        submission that named neither a chapter nor a section carries no
        chapter identity there is any way to leak.
        """
        path = self.tree.unrouted_path
        return path if path.exists() else None

    def for_chapter(self) -> Path | None:
        """This chapter's own file — readers writing about it whole.

        Nothing where the run is not one chapter: the file is addressed by
        chapter ordinal, and a run whose plan spans chapters or names none has
        no address to read.
        """
        if self.chapter is None:
            return None
        path = self.tree.chapter_path(self.chapter)
        return path if path.exists() else None

    def chapter_sections(self) -> list[AddressedFile]:
        """Every section file filed under this run's chapter, by its ordinal.

        What a stage reads when it has a chapter but no section titles to
        match against yet. A run that is not one chapter matches nothing here
        without needing a guard — a filed address always carries a chapter, so
        it can never equal the nothing an unplaced run holds.
        """
        return [
            filed
            for filed in self.tree.addressed_files()
            if filed.address.chapter == self.chapter
        ]

    def for_section(self, title: str) -> Path | None:
        """The file for one plan section, where readers wrote about it.

        Resolved by the whole address — the chapter the plan places the
        section in, and the section's own ordinal — so a run reaches its own
        chapter's readers and no others. A section the plan places in no
        chapter gets nothing, and so does a section the plan does not place:
        an export spans chapters, every section ordinal in it belongs to
        several of them, and an ordinal on its own picks one of those chapters
        at random.

        Every "nothing" here is deliberate: handing a writer another
        section's readers is worse than handing them none, and the stage still
        has the index and the unrouted file either way.
        """
        entry = self.addresses.entry_for(title)
        if entry is None or entry.chapter is None:
            return None
        path = self.tree.section_path(
            SectionAddress(chapter=entry.chapter, section=entry.section)
        )
        return path if path.exists() else None

    def sections(self) -> list[SectionFeedbackFile]:
        """Every plan section that has a file, in the plan's own order."""

        def found() -> Iterator[SectionFeedbackFile]:
            """Each section the same lookup a single writer would make resolves."""
            for entry in self.addresses.entries:
                if (path := self.for_section(entry.title)) is not None:
                    yield SectionFeedbackFile(title=entry.title, path=path)

        return list(found())
