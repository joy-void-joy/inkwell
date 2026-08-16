"""Content contract: metadata envelopes and typed file references.

Every piece of content the pipeline produces — source extracts, voice
analyses, section drafts, merged articles — gets a ContentEnvelope that
carries identity, size, and navigable structure. The envelope does NOT
hold content itself; it points to markdown files on disk.

ContentRef and ContentManifest provide typed, role-annotated file
references for pipeline stage inputs. Agents see the role tag before
reading a file, so they know its purpose without guessing.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

ContentRole = Literal[
    "source",
    "plan",
    "research",
    "voice_analysis",
    "prescriptive_rules",
    "style_reference",
    "draft",
    "feedback",
    "reader_feedback",
    "review",
]


class ContentSection(BaseModel):
    """A navigable section within a content artifact."""

    heading: str
    offset: int = Field(description="Line offset within the file (0-based)")
    line_count: int
    word_count: int


class ContentEnvelope(BaseModel):
    """Metadata wrapper for a pipeline content artifact."""

    label: str = Field(description="Human-readable identity")
    stage: str = Field(description="Pipeline stage that produced this")
    content_type: str = Field(
        description="Category: source, voice_analysis, prescriptive, draft, research, review"
    )
    path: str = Field(description="Primary file path")
    word_count: int = 0
    char_count: int = 0
    sections: list[ContentSection] = Field(
        default_factory=list,
        description="Navigable sections (empty for small or unstructured content)",
    )
    extra_paths: list[str] = Field(
        default_factory=list,
        description="Overflow chunk paths for content split across files",
    )


MARKDOWN = MarkdownIt()
"""Reads content as the blocks its author wrote, so which lines are headings
is the parser's business rather than a '#' prefix matched by hand — and a '#'
inside a code fence is not one."""

DEEPEST_NAVIGABLE_HEADING = 3
"""Headings deeper than this are body structure, not a section to navigate to."""


class HeadingAt(BaseModel):
    """One heading of a document, and where in it the heading sits."""

    line: int = Field(description="Zero-based line the heading is written on")
    depth: int = Field(description="Heading level: 1 for '#', 2 for '##'")
    text: str = Field(description="The heading's text, without its markers")


def headings_in(content: str) -> list[HeadingAt]:
    """Every heading of `content`, in document order."""

    def found() -> Iterator[HeadingAt]:
        depth = 0
        line = 0
        for token in MARKDOWN.parse(content):
            if token.type == "heading_open":
                depth = int(token.tag.removeprefix("h"))
                line = token.map[0] if token.map else 0
            elif token.type == "inline" and depth:
                yield HeadingAt(line=line, depth=depth, text=token.content.strip())
                depth = 0

    return list(found())


def extract_sections(content: str) -> list[ContentSection]:
    """Extract navigable sections from markdown content.

    Splits on # through ### headings. Each section runs from its heading to
    the next one at that depth or shallower (or the end of the content).
    """
    lines = content.splitlines()
    headings = [
        heading
        for heading in headings_in(content)
        if heading.depth <= DEEPEST_NAVIGABLE_HEADING
    ]

    def section(index: int, heading: HeadingAt) -> ContentSection:
        """One heading, and everything written under it up to the next."""
        end = headings[index + 1].line if index + 1 < len(headings) else len(lines)
        text = "\n".join(lines[heading.line : end])
        return ContentSection(
            heading=heading.text,
            offset=heading.line,
            line_count=end - heading.line,
            word_count=len(text.split()),
        )

    return [section(index, heading) for index, heading in enumerate(headings)]


def build_envelope(
    label: str,
    stage: str,
    content_type: str,
    path: str | Path,
    content: str,
    extra_paths: list[str] | None = None,
) -> ContentEnvelope:
    """Build a ContentEnvelope from content text.

    Computes word/char counts and extracts markdown sections automatically.
    """
    return ContentEnvelope(
        label=label,
        stage=stage,
        content_type=content_type,
        path=str(path),
        word_count=len(content.split()),
        char_count=len(content),
        sections=extract_sections(content),
        extra_paths=extra_paths or [],
    )


class ContentRef(BaseModel):
    """A file reference with semantic role annotation."""

    path: str
    role: ContentRole
    label: str
    word_count: int = 0
    instruction: str = ""

    def render_line(self) -> str:
        wc = f" ({self.word_count} words)" if self.word_count else ""
        inst = f" — {self.instruction}" if self.instruction else ""
        return f"- [{self.role}] {self.label}{wc}: {self.path}{inst}"


class ContentManifest(BaseModel):
    """Typed set of file references for a pipeline stage."""

    refs: list[ContentRef] = Field(default_factory=list)

    def add(
        self,
        path: str | Path,
        role: ContentRole,
        label: str,
        *,
        word_count: int = 0,
        instruction: str = "",
    ) -> None:
        self.refs.append(
            ContentRef(
                path=str(path),
                role=role,
                label=label,
                word_count=word_count,
                instruction=instruction,
            )
        )

    def render(self) -> str:
        if not self.refs:
            return ""
        lines = ["## Input files\n"]
        for ref in self.refs:
            lines.append(ref.render_line())
        return "\n".join(lines)
