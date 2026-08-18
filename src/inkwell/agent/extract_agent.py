"""Agent-driven source extraction.

A single orchestrator agent ingests the raw pipeline inputs — URLs, file paths,
and freeform text with instructions mixed in — routes each source by role,
calls the extraction tools (which write the verbatim content to disk), and
returns a manifest describing every extracted file plus the author's
instructions and deliverables.

The agent only ever records metadata: the extraction tools own the content, so
source text is never paraphrased or summarized by the orchestrator. The single
exception is inline prose pasted directly into the input, which the agent
writes through verbatim because no tool can fetch it.
"""

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.mcp import McpServerEntry
from lup.runtime.usage import CostAccumulator
from inkwell.agent.client import query
from lup.telemetry.trace import TraceLogger

from inkwell.agent.config import stage_model
from inkwell.agent.models import SourceRole
from inkwell.agent.stages import EXTRACTOR_PROMPT
from inkwell.pdf import reads_by_page

logger = logging.getLogger(__name__)

EXTRACTOR_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob"]


class ExtractedSource(BaseModel):
    """One routed, extracted source and the file holding its content."""

    raw_input: str = Field(
        description="The original source string exactly as the author gave it — "
        "a URL, a file path, or the inline text it was drawn from"
    )
    role: SourceRole = Field(
        description="'source' = primary content to write from; "
        "'revision_target' = the piece this run rewrites, which the draft "
        "replaces rather than draws from; "
        "'style_reference' = writing whose voice to emulate; "
        "'context' = background only, mentioned but not a primary input"
    )
    origin: Literal["url", "file", "inline"] = Field(
        description="Where the content came from: a fetched 'url', a local "
        "'file', or 'inline' prose pasted directly into the input"
    )
    local_path: str = Field(
        default="",
        description="Absolute path to the verbatim content file an extraction "
        "tool wrote (its returned 'path'), or that you wrote for inline prose. "
        "Empty only when the source could not be recovered.",
    )
    title: str = Field(
        default="", description="Human-readable title of the source, if known"
    )
    note: str = Field(
        default="",
        description="Short status note when a source was only partially "
        "recovered or could not be recovered — say what you tried",
    )


class ExtractionManifest(BaseModel):
    """The orchestrator's structured account of one extraction pass."""

    instructions: str = Field(
        default="",
        description="The author's directions drawn from freeform text — what "
        "they want done, stripped of URLs. Empty if the input was bare "
        "URLs/paths with no surrounding prose.",
    )
    deliverables: list[str] = Field(
        default_factory=list,
        description="Concrete outputs the instructions ask for, one per item, "
        "preserving the author's own terms. Empty when none are named.",
    )
    sources: list[ExtractedSource] = Field(
        default_factory=list,
        description="Every source found in the input, each routed by role with "
        "the path to its extracted content",
    )
    references_absent_source: bool = Field(
        default=False,
        description="True when the instructions presuppose source material (a "
        "document, attachment, paper, prior draft) that is not present among the "
        "extracted sources.",
    )
    absent_source_note: str = Field(
        default="",
        description="What the instructions reference that appears missing, and "
        "the phrasing that signals it. Empty when nothing is missing.",
    )

    @property
    def has_concrete_source(self) -> bool:
        """Whether any primary source is a real fetched document — a URL or a
        file — rather than inline prose standing in for content.

        The absent-source guard keys off this: when the instructions point at
        source material but every primary source is inline prose (the directions
        themselves), the actual document never arrived.
        """
        return any(
            entry.role in ("source", "revision_target")
            and entry.origin in ("url", "file")
            for entry in self.sources
        )


SOURCE_BLOCK_LABELS: dict[SourceRole, str] = {
    "source": "Source",
    "revision_target": "Draft being revised",
    "style_reference": "Reference",
    "context": "Reference",
}
"""How each role announces itself at the head of its block in the source text.

The revision target says what it is, because a stage that reads the assembled
text and cannot tell the draft from its references treats the piece it was
asked to replace as one more thing to write from.
"""


def guess_origin(value: str) -> Literal["url", "file", "inline"]:
    """Classify a raw input string by surface form for fallback routing."""
    stripped = value.strip()
    if stripped.startswith(("http://", "https://")):
        return "url"
    if stripped.startswith(("/", "~", "./")):
        return "file"
    return "inline"


def read_source_content(entry: ExtractedSource) -> str:
    """Read an extracted source's verbatim text from the file it points to.

    Inline-prose sources carry their content in ``raw_input`` when the agent
    wrote no file. Returns an empty string when nothing is readable, signalling
    the caller to fall back to deterministic extraction for that source. Bytes
    that are not text answer the same way: what encoding a source file turned
    out to be in is a fact about that one source, never a reason to end the run.
    """
    if entry.local_path:
        path = Path(entry.local_path)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                logger.warning("Unreadable source file %s", path, exc_info=True)
                return ""
    if entry.origin == "inline":
        return entry.raw_input
    return ""


class AssembledSources(BaseModel):
    """What a manifest yields: the blocks to read, the documents to navigate,
    and what could not be read at all."""

    blocks: list[str] = Field(
        default_factory=list,
        description="Per-source sections ready to join into the conversation",
    )
    documents: list[str] = Field(
        default_factory=list,
        description="Paths to documents read a page-window at a time rather "
        "than inlined — for the caller to hand to the source registry",
    )
    unrecovered: list[str] = Field(
        default_factory=list,
        description="Raw inputs whose content the caller must re-extract",
    )


def assemble_sources(manifest: ExtractionManifest) -> AssembledSources:
    """Sort a manifest's entries into text to inline, documents, and gaps.

    A block is one ``--- Source: ... ---`` / ``--- Reference: ... ---``
    section; an entry whose content could not be read is unrecovered instead,
    for the caller to deterministically re-extract.

    A PDF is neither: it is read a page-window at a time by a reader holding
    ``Read``, so it reaches the pipeline as a registered document and is never
    inlined. Calling it unrecovered instead would send the caller back to the
    network to re-fetch a document already sitting on disk.

    Style references are skipped: their voice feeds the voice stage through a
    separate channel, never the content blob.
    """

    def source_block(entry: ExtractedSource) -> str:
        """One entry as the conversation reads it, empty when nothing was read."""
        text = read_source_content(entry)
        if not text.strip():
            return ""
        return f"--- {SOURCE_BLOCK_LABELS[entry.role]}: {entry.raw_input} ---\n\n{text}"

    def navigable_document(entry: ExtractedSource) -> Path | None:
        """The document this entry points at, when it is one to read by page."""
        if not entry.local_path:
            return None
        path = Path(entry.local_path)
        return path if path.is_file() and reads_by_page(path) else None

    considered = [e for e in manifest.sources if e.role != "style_reference"]
    sorted_out = [(e, navigable_document(e)) for e in considered]
    read = [(e, source_block(e)) for e, doc in sorted_out if doc is None]
    return AssembledSources(
        blocks=[block for _, block in read if block],
        documents=[str(doc) for _, doc in sorted_out if doc is not None],
        unrecovered=[entry.raw_input for entry, block in read if not block],
    )


def material_role_note(material_role: SourceRole) -> str:
    """What the task adds when the entry point already knows its material's role.

    Empty for ``source``, which is what routing decides on its own anyway.
    """
    if material_role != "revision_target":
        return ""
    return (
        "\n\nThis run was launched to REVISE. The substantive content among "
        "these inputs is a draft the author wants rewritten and replaced — "
        "route it `revision_target`, not `source`. It is published or "
        "finished prose, so being polished and carrying its own citations is "
        "what a revision target looks like, never evidence that it is a "
        "reference to write from."
    )


async def run_extraction_agent(
    inputs: list[str],
    output_dir: Path,
    *,
    source_servers: dict[str, McpServerEntry],
    source_tool_names: list[str],
    material_role: SourceRole = "source",
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ExtractionManifest:
    """Route and extract every input through one orchestrator agent.

    The agent classifies each source's role, extracts the author's
    instructions, and calls the extraction tools to write each source's content
    to disk, returning a manifest of the results. Returns an empty manifest for
    empty input, and a best-effort manifest (every input treated as a source)
    when the agent yields no structured output, so the caller can still fall
    back to deterministic extraction per source.

    ``material_role`` is what the entry point already knows its material to be,
    so a ``revise`` run does not depend on the agent inferring that a chapter it
    was handed is the piece being replaced rather than one to write from.
    """
    if not inputs:
        return ExtractionManifest()

    joined = "\n\n".join(f"<input>\n{value}\n</input>" for value in inputs)
    task = (
        f"The author handed these inputs to a writing pipeline. Each <input> is "
        f"a URL, a local file path, or freeform text with instructions and "
        f"links mixed together:\n\n{joined}\n\n"
        f"Route every source by role, pull out the author's instructions and "
        f"deliverables, and extract each source's full content to its own file. "
        f"Write any inline-prose sources to files under {output_dir}. "
        f"Return one manifest entry per source." + material_role_note(material_role)
    )

    result = await query(
        task,
        output_type=ExtractionManifest,
        model=stage_model("extract"),
        system_prompt=EXTRACTOR_PROMPT,
        tools=EXTRACTOR_TOOLS,
        allowed_tools=source_tool_names,
        mcp_servers=source_servers,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        prefix="[extract] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if result is None:
        return ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input=value, role=material_role, origin=guess_origin(value)
                )
                for value in inputs
            ]
        )
    return result
