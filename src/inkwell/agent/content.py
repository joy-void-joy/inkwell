"""Content contract: metadata envelopes for pipeline artifacts.

Every piece of content the pipeline produces — source extracts, voice
analyses, section drafts, merged articles — gets a ContentEnvelope that
carries identity, size, and navigable structure. The envelope does NOT
hold content itself; it points to markdown files on disk.
"""

import re
from pathlib import Path

from pydantic import BaseModel, Field


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


HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def extract_sections(content: str) -> list[ContentSection]:
    """Extract navigable sections from markdown content.

    Splits on ## and ### headings. Each section runs from its heading
    to the next heading of equal or higher level (or end of content).
    """
    lines = content.split("\n")
    matches: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) <= 3:
            matches.append((i, m.group(2).strip()))

    if not matches:
        return []

    sections: list[ContentSection] = []
    for idx, (line_offset, heading) in enumerate(matches):
        if idx + 1 < len(matches):
            end = matches[idx + 1][0]
        else:
            end = len(lines)
        section_lines = lines[line_offset:end]
        section_text = "\n".join(section_lines)
        sections.append(
            ContentSection(
                heading=heading,
                offset=line_offset,
                line_count=end - line_offset,
                word_count=len(section_text.split()),
            )
        )
    return sections


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
