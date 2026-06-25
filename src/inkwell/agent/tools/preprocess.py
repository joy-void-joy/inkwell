"""Freeform input preprocessing — classify sources and extract author intent.

Always runs as the first pipeline stage. Classifies each source by role
(primary content, style reference, supplementary context) and extracts
the author's instructions from freeform text.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from inkwell.agent.tools.extract import DiscoveredLink, discover_links
from inkwell.agent.config import stage_model
from lup.client import query


class ClassifiedSource(BaseModel):
    value: str = Field(description="The source string (URL, file path, or raw text)")
    role: Literal["source", "style_reference", "context"] = Field(
        description=(
            "'source' = primary content to transform/reformulate; "
            "'style_reference' = writing style to emulate; "
            "'context' = supplementary link mentioned but not a primary input"
        )
    )
    discovered_urls: list[str] = Field(
        default_factory=list,
        description="URLs discovered within this source string (for freeform text)",
    )


class PreprocessResult(BaseModel):
    """Output of the preprocess stage — available to all downstream stages."""

    raw_inputs: list[str] = Field(
        description="Original source strings exactly as the user provided them"
    )
    instructions: str = Field(
        default="",
        description=(
            "The user's directions extracted from freeform text — "
            "what they want done, stripped of URLs. Empty if input was bare URLs."
        ),
    )
    deliverables: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete outputs the instructions ask for, one per item, "
            "preserving the author's own terms (scope, setting, format). "
            "Empty when the instructions name none."
        ),
    )
    references_absent_source: bool = Field(
        default=False,
        description=(
            "True when the instructions clearly refer to specific source "
            "material (a document, attachment, paper, prior draft) that is not "
            "present among the provided sources."
        ),
    )
    absent_source_note: str = Field(
        default="",
        description=(
            "What the instructions reference that appears to be missing, and the "
            "phrasing that signals it. Empty when nothing is missing."
        ),
    )
    classified: list[ClassifiedSource] = Field(
        description="Each source with its classified role"
    )

    @property
    def sources(self) -> list[str]:
        """URLs/paths classified as primary source material."""
        results: list[str] = []
        for c in self.classified:
            if c.role == "source":
                if c.discovered_urls:
                    results.extend(c.discovered_urls)
                else:
                    results.append(c.value)
        return results

    @property
    def style_refs(self) -> list[str]:
        """URLs classified as style references."""
        results: list[str] = []
        for c in self.classified:
            if c.role == "style_reference":
                if c.discovered_urls:
                    results.extend(c.discovered_urls)
                else:
                    results.append(c.value)
        return results

    @property
    def context_refs(self) -> list[str]:
        """URLs classified as supplementary context."""
        results: list[str] = []
        for c in self.classified:
            if c.role == "context":
                if c.discovered_urls:
                    results.extend(c.discovered_urls)
                else:
                    results.append(c.value)
        return results

    @property
    def has_concrete_source(self) -> bool:
        """Whether any primary source is a real document — a URL or a file on
        disk — rather than freeform prose standing in for content.

        The guard against a referenced-but-missing document keys off this:
        when the instructions point at source material but every source is
        plain text, the actual document never arrived.
        """
        for c in self.classified:
            if c.role != "source":
                continue
            value = c.value.strip()
            if c.discovered_urls or value.startswith(("http://", "https://")):
                return True
            if Path(value).is_file():
                return True
        return False


CLASSIFY_SYSTEM = """\
You classify sources provided to a writing pipeline.

The user provided one or more inputs — bare URLs, file paths, or freeform text \
with embedded URLs. Your job: determine the ROLE of each source.

Roles:
- **source**: The primary content to be transformed, reformulated, or written about. \
This is the "input" material. If only one URL is provided with no other context, \
it's a source.
- **style_reference**: A piece of writing whose STYLE the user wants to emulate. \
Look for phrases like "in the style of", "write like", "voice of", "tone of".
- **context**: A link mentioned for background but not as primary source or style \
target. Look for "see also", "related", "for reference".

Also extract the user's INSTRUCTIONS — what they want done with the material. \
This is the prose around the URLs, preserving their exact wording. If the input \
is just a bare URL with no surrounding text, instructions should be empty.

From the instructions, also list the DELIVERABLES: each concrete output the \
user asked for, one item per deliverable, in the user's own terms. Keep their \
scope words exactly (a requested setting, logic, audience, or format is part \
of the deliverable, not a suggestion). Empty list if no concrete outputs are \
named.

Finally, judge whether the instructions PRESUPPOSE source material that is not \
present. Authors often paste directions that lean on "the document", "the \
attached file", "the paper", "what I'm reading", or a prior draft — wording \
that only makes sense if that material were provided. If the instructions \
depend on such material yet no source carries it (every source is the \
directions themselves), set references_absent_source and note what is missing. \
If the author is writing from scratch with no such dependency, leave it false."""


class ClassifyOutput(BaseModel):
    instructions: str = Field(
        description=(
            "The user's directions extracted from the text — "
            "what they want done. Empty string if input is just bare URLs."
        )
    )
    deliverables: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete outputs the instructions ask for, one per item, "
            "preserving the author's own terms"
        ),
    )
    references_absent_source: bool = Field(
        default=False,
        description=(
            "True when the instructions presuppose source material (a document, "
            "attachment, paper, prior draft) that is not present among the sources."
        ),
    )
    absent_source_note: str = Field(
        default="",
        description="What is referenced but appears missing, and the phrasing that signals it.",
    )
    classified: list[ClassifiedSource] = Field(
        description="Each source with its classified role"
    )


async def preprocess_sources(sources: list[str]) -> PreprocessResult:
    """Classify all pipeline sources by role and extract instructions.

    For simple cases (single bare URL), skips the LLM call and classifies directly.
    For freeform text with embedded URLs, uses an LLM to classify roles.
    """
    if not sources:
        return PreprocessResult(raw_inputs=[], instructions="", classified=[])

    all_simple = all(is_bare_url_or_path(s) for s in sources)
    if all_simple:
        classified = [ClassifiedSource(value=s, role="source") for s in sources]
        return PreprocessResult(
            raw_inputs=sources, instructions="", classified=classified
        )

    combined = "\n\n".join(sources)
    links = discover_links(combined)

    source_descriptions = build_source_descriptions(sources, links)

    result = await query(
        f"User inputs:\n\n{combined}\n\n"
        f"Discovered sources:\n{source_descriptions}\n\n"
        f"Classify each source's role and extract the user's instructions.",
        model=stage_model("preprocess"),
        system_prompt=CLASSIFY_SYSTEM,
        output_type=ClassifyOutput,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        tools=[],
    )

    if result is None:
        classified = [
            ClassifiedSource(
                value=s,
                role="source",
                discovered_urls=[link.url for link in links if link.url in s],
            )
            for s in sources
        ]
        return PreprocessResult(
            raw_inputs=sources, instructions="", classified=classified
        )

    return PreprocessResult(
        raw_inputs=sources,
        instructions=result.instructions,
        deliverables=result.deliverables,
        references_absent_source=result.references_absent_source,
        absent_source_note=result.absent_source_note,
        classified=result.classified,
    )


def is_bare_url_or_path(s: str) -> bool:
    """Check if a string is a single URL or file path with no surrounding prose."""
    stripped = s.strip()
    if "\n" in stripped:
        return False
    if stripped.startswith(("http://", "https://")):
        return " " not in stripped
    if stripped.startswith(("/", "~", "./")):
        return " " not in stripped
    return False


def build_source_descriptions(sources: list[str], links: list[DiscoveredLink]) -> str:
    """Build a description of sources for the classifier prompt."""
    lines: list[str] = []
    for i, src in enumerate(sources, 1):
        src_links = [link for link in links if link.url in src]
        if src_links:
            urls = ", ".join(f"{link.url} ({link.link_type})" for link in src_links)
            lines.append(f"{i}. Contains URLs: {urls}")
        elif is_bare_url_or_path(src):
            lines.append(f"{i}. Bare URL/path: {src}")
        else:
            preview = src[:100] + "..." if len(src) > 100 else src
            lines.append(f"{i}. Freeform text: {preview!r}")
    return "\n".join(lines)
