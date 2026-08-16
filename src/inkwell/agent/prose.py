"""Reading a draft as the structure its author wrote.

Every check a format declares reads prose through this module rather than
walking characters. Blocks come from the markdown parser, so a heading is a
heading and a '#' inside a code fence is not. Sentences — and the grammatical
facts a row needs about them — come from the segmenter behind `ProseReader`,
derived once at the parse so no row re-derives what the parse already knows.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from statistics import pstdev

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import BaseModel, Field

MARKDOWN = MarkdownIt()
"""Reads content as the blocks its author wrote, so a heading is the parser's
business rather than a '#' prefix somebody strips by hand."""


def strong_runs(inline: Token) -> list[str]:
    """The text of every bold run in a block, in document order.

    Read off the inline token stream, so a row counting how much of a draft is
    set in bold counts what a reader will see emphasized rather than pairs of
    asterisks. Markdown does not nest emphasis, so each open pairs with the
    close at its own position.
    """
    children = inline.children or []
    opens = [i for i, token in enumerate(children) if token.type == "strong_open"]
    closes = [i for i, token in enumerate(children) if token.type == "strong_close"]
    return [
        "".join(child.content for child in children[start + 1 : end]).strip()
        for start, end in zip(opens, closes)
        if end > start
    ]


def leading_strong(inline: Token) -> str:
    """The text of a bold run at the very start of a block, or empty.

    Read off the inline token stream rather than off a '**' prefix, so
    emphasis inside a word and a bold run further into the paragraph both
    read as what they are. markdown-it opens an inline run with an empty text
    token, so the search starts at the first token carrying anything.
    """
    children = inline.children or []
    opening = next(
        (
            index
            for index, token in enumerate(children)
            if token.type != "text" or token.content
        ),
        None,
    )
    if opening is None or children[opening].type != "strong_open":
        return ""
    runs = strong_runs(inline)
    return runs[0] if runs else ""


def plain_text(inline: Token) -> str:
    """A block's prose with its inline markers gone.

    Taken off the token stream, so the segmenter reads the sentence the
    reader sees rather than one carrying '**' and '[]()' as though they were
    words.
    """
    children = inline.children
    if children is None:
        return inline.content

    def spans() -> Iterator[str]:
        """Each child's contribution to the rendered prose."""
        for token in children:
            match token.type:
                case "softbreak" | "hardbreak":
                    yield " "
                case "text" | "code_inline":
                    yield token.content

    return "".join(spans()).strip()


class MarkdownBlock(BaseModel):
    """One top-level block of a markdown document, and what kind it is."""

    kind: str = Field(description="Block type, as markdown-it names it")
    text: str = Field(description="The block's text, without the markers around it")
    plain: str = Field(
        description="The block's prose with inline markers gone — what a "
        "segmenter reads, as against the markdown a formatter re-emits"
    )
    source: str = Field(description="The block exactly as the author wrote it")
    bold_opening: str = Field(
        default="",
        description="Text of the bold run this block opens with, empty if it "
        "does not open with one",
    )
    bold_runs: list[str] = Field(
        default_factory=list,
        description="Text of every bold run in the block, in document order",
    )

    @property
    def is_heading(self) -> bool:
        """Whether this block is a heading rather than prose."""
        return self.kind == "heading"


def markdown_blocks(content: str) -> list[MarkdownBlock]:
    """The top-level blocks `content` is written in, in document order.

    A heading arrives as a heading rather than as prose that happens to start
    with a '#', which is also what keeps a '#' inside a code fence from
    reading as one.
    """
    lines = content.splitlines()

    def blocks() -> Iterator[MarkdownBlock]:
        kind = ""
        for token in MARKDOWN.parse(content):
            if token.type.endswith("_open"):
                kind = token.type.removesuffix("_open")
            elif token.type == "inline" and token.content.strip():
                start, end = token.map if token.map else (0, len(lines))
                yield MarkdownBlock(
                    kind=kind,
                    text=token.content.strip(),
                    plain=plain_text(token),
                    source="\n".join(lines[start:end]).strip(),
                    bold_opening=leading_strong(token),
                    bold_runs=strong_runs(token),
                )

    return list(blocks())


class Sentence(BaseModel):
    """One sentence, as the token runs a declared row reads it.

    Segmentation happens once, where the parse is, and every row reads these
    same tokens. A row looking for a construction rather than a word — a comma
    followed by a gerund, an inflated copula standing in for `is` — matches it
    against `tokens` by position, which is why no row tokenizes prose itself
    and two rows cannot disagree about where a sentence starts.
    """

    text: str = Field(description="The sentence as the author wrote it")
    tokens: list[str] = Field(
        description="Every token, lowered and in order with punctuation kept — "
        "what a row matching a construction by position reads"
    )
    words: list[str] = Field(
        description="The tokens carrying a word, lowered — what a declared "
        "phrase matches against and what the sentence's length counts"
    )
    opening: str = Field(
        description="The first two words, lowered and joined — the shape that "
        "repeats when paragraph openings turn formulaic"
    )

    @property
    def length(self) -> int:
        """How many words the sentence runs to."""
        return len(self.words)


class Paragraph(BaseModel):
    """One prose block of a draft, and the sentences in it."""

    block: MarkdownBlock
    sentences: list[Sentence]
    summary_is_bolded: bool = Field(
        default=False,
        description="Whether a bold run carries this paragraph's whole first "
        "sentence, which is the convention a reader follows on the bold alone",
    )

    @property
    def length(self) -> int:
        """How many words the paragraph runs to."""
        return sum(sentence.length for sentence in self.sentences)

    @property
    def opening(self) -> str:
        """The opening shape of the paragraph's first sentence."""
        return self.sentences[0].opening if self.sentences else ""


class Section(BaseModel):
    """A heading and the prose under it, up to the next heading."""

    heading: str = Field(description="The heading's text, or empty before the first")
    paragraphs: list[Paragraph]

    @property
    def length(self) -> int:
        """How many words the section runs to."""
        return sum(paragraph.length for paragraph in self.paragraphs)


class Prose(BaseModel):
    """A whole draft, read as the blocks and sentences it is written in.

    This is what every declared row reads. It carries no parser and no
    segmenter — by the time a row sees it, the reading is done.
    """

    blocks: list[MarkdownBlock]
    paragraphs: list[Paragraph]
    sections: list[Section]

    @property
    def sentences(self) -> list[Sentence]:
        """Every sentence of the draft, in document order."""
        return [s for paragraph in self.paragraphs for s in paragraph.sentences]

    @property
    def length(self) -> int:
        """How many words the draft runs to."""
        return sum(paragraph.length for paragraph in self.paragraphs)

    def marks(self, marks: Sequence[str]) -> int:
        """How many of these punctuation marks the draft's rendered prose carries.

        Counted off `plain`, so a mark that was markup — the dashes in a table
        rule, a parenthesis inside a link target — is not counted as one the
        author reached for mid-sentence.
        """
        return sum(
            paragraph.block.plain.count(mark)
            for paragraph in self.paragraphs
            for mark in marks
        )


def spread(lengths: list[int]) -> float:
    """The standard deviation of a run of lengths, zero when too short.

    A draft of one sentence has no rhythm to measure, so it reads as no
    spread rather than as an error every caller has to special-case.
    """
    return pstdev(lengths) if len(lengths) > 1 else 0.0


class Segmenter(ABC):
    """Turns prose into the sentences and words a declared row reads.

    An injected seam: a row never holds one, it reads the `Prose` that
    `ProseReader` built with one. Which implementation fills it decides how
    much a row can know about a sentence — boundaries and tokens from a
    tokenizing segmenter, or boundaries, lemmas, and part of speech from a
    parser — without any row above it changing.
    """

    @abstractmethod
    def sentences(self, text: str) -> list[Sentence]:
        """The sentences of one prose block, each as the tokens it is made of."""

    @abstractmethod
    def words(self, text: str) -> list[str]:
        """A fragment's word forms, lowered — how a declared phrase is matched."""


class ProseReader:
    """Reads a draft into the `Prose` every declared row is checked against.

    Composes the markdown parser with whichever `Segmenter` fills the
    sentence seam.
    """

    def __init__(self, segmenter: Segmenter) -> None:
        self.segmenter = segmenter

    def read(self, content: str) -> Prose:
        """`content` as its blocks, paragraphs, sentences, and sections.

        Each block is segmented once and shared between the flat paragraph
        run and the sections, so a draft costs one parse rather than two.
        """
        blocks = markdown_blocks(content)
        parsed = [
            None if block.is_heading else self.paragraph(block) for block in blocks
        ]
        return Prose(
            blocks=blocks,
            paragraphs=[p for p in parsed if p is not None],
            sections=self.sections(blocks, parsed),
        )

    def paragraph(self, block: MarkdownBlock) -> Paragraph:
        """One block read as sentences, and whether a bold run opens it.

        The bold run has to carry the whole first sentence for the summary to
        count as bolded: a paragraph opening `**Three** reasons follow.` bolds
        a word, where the convention asks for a claim a reader can follow on
        its own.
        """
        sentences = self.segmenter.sentences(block.plain)
        return Paragraph(
            block=block,
            sentences=sentences,
            summary_is_bolded=bool(
                block.bold_opening
                and sentences
                and self.segmenter.words(block.bold_opening) == sentences[0].words
            ),
        )

    def sections(
        self, blocks: list[MarkdownBlock], parsed: list[Paragraph | None]
    ) -> list[Section]:
        """The draft's paragraphs grouped under the heading each falls beneath.

        Every heading opens a section regardless of its level, which is what a
        row measuring how long a reader goes without one is asking about.
        """
        breaks = [i for i, block in enumerate(blocks) if block.is_heading]
        opens = breaks if breaks and breaks[0] == 0 else [-1, *breaks]
        ends = [*opens[1:], len(blocks)]

        def grouped() -> Iterator[Section]:
            """One section per heading, plus any prose standing before the first."""
            for start, end in zip(opens, ends):
                body = [p for p in parsed[start + 1 : end] if p is not None]
                if start < 0 and not body:
                    continue
                yield Section(
                    heading=blocks[start].text if start >= 0 else "",
                    paragraphs=body,
                )

        return list(grouped())

    def sentences(self, text: str) -> list[Sentence]:
        """The sentences of a fragment, for a caller with no block structure.

        Segments the text as given, so a caller splitting markdown at a
        sentence boundary gets the markers back in the pieces it re-emits.
        """
        return self.segmenter.sentences(text)

    def phrase(self, text: str) -> list[str]:
        """A declared phrase as the words it is matched by."""
        return self.segmenter.words(text)
