"""Citation formatting tools.

Transforms research sources into formatted bibliographies and in-text
citation mappings. Called by the rewriter to add references appropriate
to the target output format.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field

from lup.mcp import lup_tool

logger = logging.getLogger(__name__)


SourceType = Literal["web", "paper", "dataset", "book", "report"]
CitationStyle = Literal["footnote", "numbered", "author-date", "url-only"]


class BibliographyEntry(BaseModel):
    title: str = Field(description="Source title")
    url: str = Field(description="Source URL")
    authors: list[str] = Field(
        default_factory=list, description="Author names (empty if unknown)"
    )
    year: str = Field(default="", description="Publication year (empty if unknown)")
    source_type: SourceType = Field(default="web", description="Type of source")
    key_excerpt: str = Field(
        default="", description="Most relevant excerpt from this source"
    )


class FormatBibliographyInput(BaseModel):
    style: CitationStyle = Field(
        description=(
            "Citation style: 'footnote' (markdown footnotes), 'numbered' ([1] refs), "
            "'author-date' (Smith 2024), 'url-only' (hyperlinked titles)"
        )
    )
    entries: list[BibliographyEntry] = Field(description="Sources to format")


class FormatBibliographyOutput(BaseModel):
    bibliography: str = Field(description="Formatted bibliography section as markdown")
    citation_map: dict[str, str] = Field(
        description="URL -> formatted in-text citation (e.g. url -> '[1]' or '(Smith, 2024)')"
    )
    entry_count: int = Field(description="Number of entries formatted")


def format_entry_label(entry: BibliographyEntry) -> str:
    """Build a human-readable label from entry metadata."""
    parts: list[str] = []
    if entry.authors:
        parts.append(", ".join(entry.authors))
    if entry.year:
        parts.append(f"({entry.year})")
    parts.append(f'"{entry.title}"')
    return " ".join(parts)


def do_format_bibliography(
    style: CitationStyle,
    entries: list[BibliographyEntry],
) -> FormatBibliographyOutput:
    """Format a list of sources into a bibliography and citation map."""
    if not entries:
        return FormatBibliographyOutput(bibliography="", citation_map={}, entry_count=0)

    bib_lines: list[str] = []
    citation_map: dict[str, str] = {}

    match style:
        case "footnote":
            for i, entry in enumerate(entries, 1):
                label = format_entry_label(entry)
                bib_lines.append(f"[^{i}]: {label} {entry.url}")
                citation_map[entry.url] = f"[^{i}]"
            bibliography = "\n".join(bib_lines)

        case "numbered":
            bib_lines.append("## References\n")
            for i, entry in enumerate(entries, 1):
                label = format_entry_label(entry)
                bib_lines.append(f"{i}. {label} {entry.url}")
                citation_map[entry.url] = f"[{i}]"
            bibliography = "\n".join(bib_lines)

        case "author-date":
            sorted_entries = sorted(
                entries,
                key=lambda e: (e.authors[0] if e.authors else e.title, e.year),
            )
            bib_lines.append("## References\n")
            for entry in sorted_entries:
                label = format_entry_label(entry)
                bib_lines.append(f"- {label} {entry.url}")
                if entry.authors and entry.year:
                    last_name = entry.authors[0].split()[-1]
                    citation_map[entry.url] = f"({last_name}, {entry.year})"
                elif entry.authors:
                    last_name = entry.authors[0].split()[-1]
                    citation_map[entry.url] = f"({last_name})"
                else:
                    citation_map[entry.url] = f'("{entry.title}")'
            bibliography = "\n".join(bib_lines)

        case "url-only":
            for entry in entries:
                citation_map[entry.url] = f"[{entry.title}]({entry.url})"
            bibliography = ""

    return FormatBibliographyOutput(
        bibliography=bibliography,
        citation_map=citation_map,
        entry_count=len(entries),
    )


@lup_tool(
    "Format research sources into a bibliography and in-text citation mappings. "
    "Use after list_sources to get all sources accumulated during research. "
    "Returns the bibliography as markdown and a URL-to-citation map the rewriter "
    "uses for in-text references. Match the citation style to the output format: "
    "footnotes for LessWrong/blog, numbered for academic, author-date for papers, "
    "url-only for memos and Twitter."
)
async def format_bibliography(
    params: FormatBibliographyInput,
) -> FormatBibliographyOutput:
    return do_format_bibliography(params.style, params.entries)


CITATION_TOOLS = [format_bibliography]
