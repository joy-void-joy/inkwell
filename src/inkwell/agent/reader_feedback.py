"""Reader feedback on already-published text, ingested one file per section.

The live channels — Google Doc comments, terminal input, tab edits — carry
only the author, and only while a run is in flight. Readers of text that
already shipped are a second channel with its own shape: an export from
whatever form collected them, read once at session start and filed as one
file per section of the work.

Both sides of that filing use the same identity: an ordinal path,
``chapter.section``, which is :class:`~inkwell.agent.book.SectionAddress` and
belongs to the book rather than to this reader — the same address a reference
in a chapter's prose resolves to, so it is declared where the ordinals are.
The export keys its rows by chapter and section number;
``SectionAddresses`` reads the same path off the plan's own sections. So a
stage revising one section opens one file, the plan stage reads the index of
all of them, and no theme drawn from any particular round of feedback is
carried in a prompt — the stage reads the evidence.

The export this was built against is a Formspree JSON dump: a metadata
header, then one row per submission — most a bare up/down vote, a minority
carrying prose.

    notes/reader/
    ├── sections/01.03.md   # one file per addressed section
    ├── unrouted.md         # submissions no section ordinal addressed
    ├── index.md            # the routed set, with a path per section
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

from inkwell.agent.book import SectionAddress
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

UNADDRESSED = "no section ordinal — chapter-level feedback, or an incomplete row"
"""Why a substantive submission ends up unrouted rather than on a section."""


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
        section and is left unrouted rather than guessed at.
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


class Unroutable(BaseModel):
    """A substantive submission no section ordinal addressed, and why not."""

    submission: ReaderSubmission
    reason: str = Field(description="What kept the submission off a section")


class SortedSubmissions(BaseModel):
    """A run's submissions, split by what ingestion can do with each."""

    routed: list[Routed] = Field(
        default_factory=list, description="Prose about an identified section"
    )
    unroutable: list[Unroutable] = Field(
        default_factory=list, description="Prose that named no section"
    )
    votes: int = Field(
        default=0, description="Bare votes and ratings, carrying no prose"
    )


def sort_submissions(
    submissions: list[ReaderSubmission], rule: SubstantiveRule
) -> SortedSubmissions:
    """Split submissions into section-addressed prose, loose prose, and votes."""
    substantive = [one for one in submissions if rule.substantive_prose(one)]
    return SortedSubmissions(
        routed=[
            Routed(submission=one, address=address)
            for one in substantive
            if (address := one.address()) is not None
        ],
        unroutable=[
            Unroutable(submission=one, reason=UNADDRESSED)
            for one in substantive
            if one.address() is None
        ],
        votes=len(submissions) - len(substantive),
    )


def by_address(routed: list[Routed]) -> dict[SectionAddress, list[ReaderSubmission]]:
    """Group routed submissions under the section address each one named."""
    ordered = sorted(routed, key=lambda entry: entry.address.key)
    return {
        address: [entry.submission for entry in group]
        for address, group in groupby(ordered, key=lambda entry: entry.address)
    }


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
    unrouted: int = Field(
        default=0, description="Substantive submissions no section addressed"
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
            f"across {self.sections} section(s), {self.votes} bare vote(s), "
            f"{self.unrouted} unroutable, {len(self.malformed)} malformed, "
            f"{len(self.skipped)} file(s) skipped"
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


def render_unrouted_file(unroutable: list[Unroutable]) -> str:
    """Substantive submissions naming no section, kept where a stage sees them."""
    header = (
        f"# Reader feedback with no section address\n\n"
        f"{len(unroutable)} substantive submission(s) whose section could not be "
        f"resolved. They are here rather than dropped: read them as feedback on "
        f"the work at large.\n"
    )

    def entries() -> Iterator[str]:
        """Each unroutable submission, with the reason it landed here."""
        for ordinal, entry in enumerate(unroutable, 1):
            yield f"{render_submission(entry.submission, ordinal)}\n\n*({entry.reason})*"

    return f"{header}\n" + "\n\n".join(entries()) + "\n"


def render_index_file(
    report: IngestionReport, routed: list[AddressedFile], unrouted: Path
) -> str:
    """The routed set as a listing: one line per section, with its file path."""

    def lines() -> Iterator[str]:
        """A header carrying the counts, then the listing as one tight block."""
        yield "# Reader feedback, by section"
        yield (
            "Readers of the already-published text, filed by the ordinal path "
            "(chapter.section) their submission named. Read the file for the "
            "section you are acting on."
        )
        yield report.summary()
        if routed:
            yield "## Sections"
            yield "\n".join(
                f"- section {entry.address.label()}: {entry.path}" for entry in routed
            )
        if report.unrouted:
            yield "## No section address"
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

    def addressed_files(self) -> list[AddressedFile]:
        """Every per-section file the last ingestion filed, by its address."""
        if not self.report_path.exists():
            return []
        report = IngestionReport.model_validate_json(
            self.report_path.read_text(encoding="utf-8")
        )
        return [
            AddressedFile(address=address, path=self.section_path(address))
            for address in report.addresses
        ]

    def clear(self) -> None:
        """Unlink what a previous ingestion filed, leaving nothing stale behind."""
        for path in self.sections_dir.glob("*.md"):
            path.unlink()
        self.unrouted_path.unlink(missing_ok=True)

    def write(
        self,
        report: IngestionReport,
        routed: dict[SectionAddress, list[ReaderSubmission]],
        unroutable: list[Unroutable],
    ) -> IngestionReport:
        """File the routed set, the unrouted remainder, the index, and the report.

        A section with no substantive submission gets no file — an empty one
        would read to a stage as "the readers said nothing here", which is a
        different claim from "nobody wrote in about this".

        What a previous ingestion filed here is cleared first, so a corrected
        export replaces the set rather than layering on top of it: a section
        whose feedback has been withdrawn must stop having a file, not keep a
        stale one that stages go on reading.
        """
        self.clear()
        self.sections_dir.mkdir(parents=True, exist_ok=True)

        def filed() -> Iterator[AddressedFile]:
            """Each addressed section, written out under its ordinal path."""
            for address in sorted(routed, key=lambda seen: seen.key):
                path = self.section_path(address)
                path.write_text(
                    render_section_file(address, routed[address]), encoding="utf-8"
                )
                yield AddressedFile(address=address, path=path)

        written = list(filed())
        if unroutable:
            self.unrouted_path.write_text(
                render_unrouted_file(unroutable), encoding="utf-8"
            )

        report.addresses = [entry.address for entry in written]
        report.filed = True
        self.index_path.write_text(
            render_index_file(report, written, self.unrouted_path), encoding="utf-8"
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
    """Read every export ``source`` names and file it one section per file.

    Substantive submissions are routed by the ordinal path they name; the ones
    that name no section land in the unrouted file, counted rather than
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
        substantive=len(split.routed) + len(split.unroutable),
        votes=split.votes,
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
    return tree.write(report, by_address(split.routed), split.unroutable)


class SectionFeedbackFile(BaseModel):
    """One plan section's reader feedback, and where it sits on disk."""

    title: str = Field(description="The plan's own title for the section")
    path: Path


class ReaderFeedback(BaseModel):
    """A run's ingested reader feedback, addressed the way the plan addresses.

    Built once per stage from the notes tree and the plan in hand, so every
    stage reaches a section's file through the same declared mapping instead
    of matching titles against text.
    """

    tree: ReaderFeedbackTree
    addresses: SectionAddresses = Field(default_factory=SectionAddresses)

    @classmethod
    def for_plan(cls, reader_dir: Path, plan: ArticlePlan | None) -> Self:
        """Read what is on disk, addressed against ``plan`` where there is one.

        The plan stage runs before any plan exists; it reads the index and the
        unrouted file, which need no mapping.
        """
        return cls(
            tree=ReaderFeedbackTree(root=reader_dir),
            addresses=SectionAddresses.for_plan(plan) if plan else SectionAddresses(),
        )

    def index(self) -> Path | None:
        """The listing of the whole routed set, once something has been filed."""
        return self.tree.index_path if self.tree.ingested() else None

    def unrouted(self) -> Path | None:
        """Feedback that named no section, where any was filed."""
        path = self.tree.unrouted_path
        return path if path.exists() else None

    def for_section(self, title: str) -> Path | None:
        """The file for one plan section, where readers wrote about it.

        A section the plan places by chapter and ordinal names a file
        outright. A section placed by position alone — the plan says which
        ordinal, not which chapter — takes the file filed under that ordinal,
        and nothing at all when several chapters have one. A section the plan
        does not place gets nothing.

        Every "nothing" here is deliberate: handing a writer another
        section's readers is worse than handing them none, and the stage still
        has the index and the unrouted file either way.
        """
        entry = self.addresses.entry_for(title)
        if entry is None:
            return None
        if entry.chapter is not None:
            path = self.tree.section_path(
                SectionAddress(chapter=entry.chapter, section=entry.section)
            )
            return path if path.exists() else None
        candidates = [
            filed
            for filed in self.tree.addressed_files()
            if filed.address.section == entry.section
        ]
        return candidates[0].path if len(candidates) == 1 else None

    def sections(self) -> list[SectionFeedbackFile]:
        """Every plan section that has a file, in the plan's own order."""

        def found() -> Iterator[SectionFeedbackFile]:
            """Each section the same lookup a single writer would make resolves."""
            for entry in self.addresses.entries:
                if (path := self.for_section(entry.title)) is not None:
                    yield SectionFeedbackFile(title=entry.title, path=path)

        return list(found())
