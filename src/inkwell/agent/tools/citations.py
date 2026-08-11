"""Citation formatting tools.

Transforms research sources into formatted bibliographies and in-text
citation mappings. Called by the rewriter to add references appropriate
to the target output format.
"""

import logging
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, Field

from lup.mcp import lup_tool
from lup.types import StringMap

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
    citation_map: StringMap = Field(
        description="URL -> formatted in-text citation (e.g. url -> '[1]' or '(Smith, 2024)')"
    )
    entry_count: int = Field(description="Number of entries formatted")


def format_entry_label(entry: BibliographyEntry) -> str:
    """Build a human-readable label from entry metadata."""

    def parts() -> Iterator[str]:
        """Authors, then year, then the title — whichever the entry carries."""
        if entry.authors:
            yield ", ".join(entry.authors)
        if entry.year:
            yield f"({entry.year})"
        yield f'"{entry.title}"'

    return " ".join(parts())


def author_date_citation(entry: BibliographyEntry) -> str:
    """How an author-date style cites this entry in the running text."""
    if not entry.authors:
        return f'("{entry.title}")'
    last_name = entry.authors[0].split()[-1]
    return f"({last_name}, {entry.year})" if entry.year else f"({last_name})"


def do_format_bibliography(
    style: CitationStyle,
    entries: list[BibliographyEntry],
) -> FormatBibliographyOutput:
    """Format a list of sources into a bibliography and citation map."""
    if not entries:
        return FormatBibliographyOutput(bibliography="", citation_map={}, entry_count=0)

    match style:
        case "footnote":
            numbered = list(enumerate(entries, 1))
            bibliography = "\n".join(
                f"[^{i}]: {format_entry_label(entry)} {entry.url}"
                for i, entry in numbered
            )
            citation_map = {entry.url: f"[^{i}]" for i, entry in numbered}

        case "numbered":
            numbered = list(enumerate(entries, 1))
            bibliography = "\n".join(
                [
                    "## References\n",
                    *(
                        f"{i}. {format_entry_label(entry)} {entry.url}"
                        for i, entry in numbered
                    ),
                ]
            )
            citation_map = {entry.url: f"[{i}]" for i, entry in numbered}

        case "author-date":
            sorted_entries = sorted(
                entries,
                key=lambda e: (e.authors[0] if e.authors else e.title, e.year),
            )
            bibliography = "\n".join(
                [
                    "## References\n",
                    *(
                        f"- {format_entry_label(entry)} {entry.url}"
                        for entry in sorted_entries
                    ),
                ]
            )
            citation_map = {
                entry.url: author_date_citation(entry) for entry in sorted_entries
            }

        case "url-only":
            bibliography = ""
            citation_map = {
                entry.url: f"[{entry.title}]({entry.url})" for entry in entries
            }

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
