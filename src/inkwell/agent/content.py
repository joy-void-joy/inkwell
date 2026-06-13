"""Content contract: metadata envelopes and typed file references.

Every piece of content the pipeline produces — source extracts, voice
analyses, section drafts, merged articles — gets a ContentEnvelope that
carries identity, size, and navigable structure. The envelope does NOT
hold content itself; it points to markdown files on disk.

ContentRef and ContentManifest provide typed, role-annotated file
references for pipeline stage inputs. Agents see the role tag before
reading a file, so they know its purpose without guessing.
"""

import re
from pathlib import Path
from typing import Literal

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
