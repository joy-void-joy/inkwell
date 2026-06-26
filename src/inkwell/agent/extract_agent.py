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

from claude_agent_sdk import McpServerConfig
from pydantic import BaseModel, Field

from lup.client import CostAccumulator, query
from lup.trace import TraceLogger

from inkwell.agent.config import stage_model
from inkwell.agent.stages import EXTRACTOR_PROMPT

logger = logging.getLogger(__name__)

EXTRACTOR_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob"]


class ExtractedSource(BaseModel):
    """One routed, extracted source and the file holding its content."""

    raw_input: str = Field(
        description="The original source string exactly as the author gave it — "
        "a URL, a file path, or the inline text it was drawn from"
    )
    role: Literal["source", "style_reference", "context"] = Field(
        description="'source' = primary content to write from; "
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
            entry.role == "source" and entry.origin in ("url", "file")
            for entry in self.sources
        )


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
    the caller to fall back to deterministic extraction for that source.
    """
    if entry.local_path:
        path = Path(entry.local_path)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    if entry.origin == "inline":
        return entry.raw_input
    return ""


def assemble_sources(manifest: ExtractionManifest) -> tuple[list[str], list[str]]:
    """Turn a manifest into verbatim source blocks plus an unrecovered list.

    Returns ``(blocks, unrecovered)``: ``blocks`` are the per-source
    ``--- Source: ... ---`` / ``--- Reference: ... ---`` sections ready to join
    into the conversation, and ``unrecovered`` holds the raw inputs whose content
    could not be read and that the caller should deterministically re-extract.

    Style references are skipped: their voice feeds the voice stage through a
    separate channel, never the content blob.
    """
    blocks: list[str] = []
    unrecovered: list[str] = []
    for entry in manifest.sources:
        if entry.role == "style_reference":
            continue
        text = read_source_content(entry)
        if text.strip():
            label = "Source" if entry.role == "source" else "Reference"
            blocks.append(f"--- {label}: {entry.raw_input} ---\n\n{text}")
        else:
            unrecovered.append(entry.raw_input)
    return blocks, unrecovered


async def run_extraction_agent(
    inputs: list[str],
    output_dir: Path,
    *,
    source_servers: dict[str, McpServerConfig],
    source_tool_names: list[str],
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
        f"Return one manifest entry per source."
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
        permission_mode="bypassPermissions",
        prefix="[extract] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if not isinstance(result, ExtractionManifest):
        return ExtractionManifest(
            sources=[
                ExtractedSource(
                    raw_input=value, role="source", origin=guess_origin(value)
                )
                for value in inputs
            ]
        )
    return result
