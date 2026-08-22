"""Voice analysis and style corpus tools.

Analyzes the author's writing style from source conversations and
reference pieces. Each source is analyzed individually and cached;
cached analyses are merged into a unified voice guide. Format-specific
reference examples (e.g. good LessWrong posts, good memos) are loaded
separately and included in the merge.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field

from lup.mcp import McpServerEntry
from lup.runtime.usage import CostAccumulator

from inkwell.agent.config import current_settings, stage_model
from inkwell.agent.client import query
from inkwell.agent.stages import format_key
from lup.telemetry.trace import TraceLogger
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Agent"]


class StyleSample(BaseModel):
    """One piece of writing the voice analysis reads, and where it came from.

    The corpus used to travel as three lists running in parallel — texts,
    labels, kinds — which every consumer had to zip back together before it
    could say anything about a sample.
    """

    label: str = Field(description="File name, URL, or the name a caller gave it")
    text: str = Field(description="The sample's prose")
    source_type: str = Field(
        default="prose", description="Which analysis hint the sample is read under"
    )

    @property
    def prescriptive(self) -> bool:
        """Whether the author wrote this as instruction rather than as a sample."""
        return self.source_type == "prescriptive"


class VoiceAnalysis(BaseModel):
    """What analyzing one sample produced, and how its source asked to be used."""

    label: str = Field(description="The sample this analysis is of")
    analysis: str = Field(
        default="", description="The analysis text, empty when none was produced"
    )
    prescriptive: bool = Field(
        default=False,
        description="Whether the source is a rule document to pass through whole",
    )

    @classmethod
    def from_cached(cls, label: str, cached: str) -> Self:
        """Read a cached analysis, taking its prescriptive flag off frontmatter.

        An analysis cached before the flag existed carries no frontmatter and
        reads as descriptive, which is what it was.
        """
        stripped = cached.strip()
        opening = "---"
        if not stripped.startswith(opening):
            return cls(label=label, analysis=cached)
        end = stripped.find(opening, len(opening))
        if end == -1:
            return cls(label=label, analysis=cached)
        frontmatter = stripped[len(opening) : end]
        body = stripped[end + len(opening) :].strip()
        return cls(
            label=label,
            analysis=body or cached,
            prescriptive="prescriptive: true" in frontmatter.lower(),
        )


class LoadCorpusInput(BaseModel):
    max_samples: int = Field(
        default=5, ge=1, le=20, description="Max style samples to return"
    )


class LoadCorpusOutput(BaseModel):
    samples: list[str] = Field(description="Style corpus text samples")
    sources: list[str] = Field(description="Source file names or URLs")
    count: int = Field(description="Number of samples returned")
    total_available: int = Field(description="Total samples in the corpus")


def text_files_in(directory: Path) -> list[Path]:
    """The prose files a style directory holds, in a stable order."""
    return sorted(directory.glob("*.md")) + sorted(directory.glob("*.txt"))


async def read_style_samples(
    directory: Path, *, source_type: str = "prose", label_prefix: str = ""
) -> list[StyleSample]:
    """Every sample a style directory holds: its prose files, and its URLs.

    A ``urls.txt`` in the directory lists references to fetch; each goes
    through the shared fetch cache, so a later run reads what is on disk
    rather than the network.
    """
    if not directory.exists():
        return []

    def local() -> Iterator[StyleSample]:
        for path in text_files_in(directory):
            if path.name == "urls.txt":
                continue
            content = path.read_text(encoding="utf-8").strip()
            if content:
                yield StyleSample(
                    label=f"{label_prefix}{path.name}",
                    text=content,
                    source_type=source_type,
                )

    async def fetched() -> AsyncIterator[StyleSample]:
        urls_file = directory / "urls.txt"
        if not urls_file.exists():
            return
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url:
                continue
            prose = await fetch_style_url(url)
            if prose:
                yield StyleSample(label=url, text=prose, source_type=source_type)

    return list(local()) + [sample async for sample in fetched()]


async def load_style_corpus() -> list[StyleSample]:
    """Load every sample from the style corpus directory.

    A prescriptive sample is a style guide, editing checklist, or other rule
    document that should be passed through verbatim rather than analyzed for
    voice; those live in config/style/prescriptive/.
    """
    style_dir = Path(current_settings().style_corpus_path)
    return await read_style_samples(style_dir) + await read_style_samples(
        style_dir / "prescriptive",
        source_type="prescriptive",
        label_prefix="prescriptive/",
    )


async def fetch_style_url(url: str) -> str | None:
    """The prose at a style-corpus URL, or nothing where it cannot be read.

    Asks for the text on every run and pays for the transfer once: the fetch
    beneath this consults the shared fetch cache, so the corpus no longer keeps
    a second copy of its own beside the style directory.
    """
    from inkwell.agent.tools.research.fetch import do_fetch_source

    try:
        result = await do_fetch_source(url)
    except Exception:
        logger.debug("Failed to fetch style corpus URL: %s", url)
        return None

    return Path(result.content.path).read_text(encoding="utf-8") or None


# ---------------------------------------------------------------------------
# Format-specific reference examples
# ---------------------------------------------------------------------------


def format_examples_dir(target_format: str) -> Path:
    """Where a format's reference examples live."""
    corpus = Path(current_settings().style_corpus_path)
    return corpus / "formats" / format_key(target_format)


async def load_format_examples(target_format: str) -> list[StyleSample]:
    """Load reference examples for a specific output format.

    Looks in config/style/formats/<format>/ for .md/.txt files and urls.txt,
    and is empty when no examples exist for this format.
    """
    return await read_style_samples(format_examples_dir(target_format))


def add_format_example(target_format: str, source: str) -> str:
    """Add a file or URL as a format-specific reference example."""
    base_format = format_key(target_format)
    format_dir = format_examples_dir(target_format)
    format_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source).expanduser()
    if source_path.exists():
        content = source_path.read_text(encoding="utf-8")
        dest = format_dir / source_path.name
        dest.write_text(content, encoding="utf-8")
        return f"Added {source_path.name} as {base_format} format example ({len(content)} chars)"

    refs_file = format_dir / "urls.txt"
    existing = refs_file.read_text(encoding="utf-8") if refs_file.exists() else ""
    if source in existing:
        return f"Already in {base_format} examples: {source}"

    with refs_file.open("a", encoding="utf-8") as f:
        f.write(source + "\n")
    return f"Added URL as {base_format} format example: {source}"


def list_format_examples(
    target_format: str | None = None,
) -> dict[str, list[StyleEntry]]:
    """List format-specific reference examples, optionally filtered to one format."""
    formats_dir = Path(current_settings().style_corpus_path) / "formats"
    if not formats_dir.exists():
        return {}

    dirs = (
        [formats_dir / format_key(target_format)]
        if target_format
        else sorted(formats_dir.iterdir())
    )
    listed = {
        fmt_dir.name: style_entries(fmt_dir) for fmt_dir in dirs if fmt_dir.is_dir()
    }
    return {name: entries for name, entries in listed.items() if entries}


@lup_tool(
    "Load the author's style corpus — writing samples they've provided "
    "as voice references. Returns text excerpts from local files and "
    "fetched URLs in config/style/. Use these samples alongside the "
    "conversation's voice notes to match the author's writing style. "
    "Call this early in the pipeline before section writing begins."
)
async def load_corpus(params: LoadCorpusInput) -> LoadCorpusOutput:
    all_samples = await load_style_corpus()
    shown = all_samples[: params.max_samples]
    samples = [sample.text for sample in shown]
    sources = [sample.label for sample in shown]

    if not samples:
        raise ToolError(
            "No style corpus found. The author can add samples with "
            "`inkwell style add <url-or-file>`."
        )

    return LoadCorpusOutput(
        samples=samples,
        sources=sources,
        count=len(samples),
        total_available=len(all_samples),
    )


# ---------------------------------------------------------------------------
# Per-source voice analysis with caching
# ---------------------------------------------------------------------------


async def compute_voice_fingerprint(
    conversation: str,
    corpus_samples: list[str],
    target_format: str = "",
) -> str:
    """Hash all voice analysis inputs to detect when re-analysis is needed."""
    h = hashlib.sha256()
    h.update(conversation.encode())
    for sample in corpus_samples:
        h.update(sample.encode())
    h.update(target_format.encode())
    fmt_samples = await load_format_examples(target_format) if target_format else []
    for fmt_sample in fmt_samples:
        h.update(fmt_sample.text.encode())
    return h.hexdigest()[:24]


def voice_cache_key(text: str) -> str:
    """Content-addressable cache key for a text source."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def voice_cache_dir() -> Path:
    """Cache directory for per-source voice analyses."""
    cache = Path(current_settings().style_corpus_path) / ".cache" / "voice"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def invalidate_merged_cache() -> None:
    """Remove the merged voice guide so it gets rebuilt from individual analyses."""
    merged = voice_cache_dir() / "merged_guide.md"
    if merged.exists():
        merged.unlink()
        logger.debug("Invalidated merged voice guide cache")


SINGLE_SOURCE_PROMPT = """\
Analyze this writing sample and produce a voice and style profile. This \
profile will be merged with analyses of other samples to create a \
comprehensive guide for writers matching the author's voice.

## Classification

First, determine whether this source is **prescriptive** — a style guide, \
editing checklist, do/don't list, or rule document that should be passed \
verbatim to writers as hard constraints. Begin your output file with YAML \
frontmatter containing a `prescriptive` field:

```
---
prescriptive: true
---
```

or:

```
---
prescriptive: false
---
```

Set `prescriptive: true` when the source is primarily a set of rules, \
instructions, or editing directives — not prose to be analyzed for voice. \
When true, still write a brief summary of the rules for the voice tab, \
but know that the raw source document will be passed directly to writers.

## Analysis (when prescriptive: false)

Focus on what makes this specific sample distinctive:

**Voice.** Tone, rhythm, sentence structure, formality, humor, hedging \
patterns, argumentation style, paragraph construction. Be evocative and \
use examples from the text — "writes like a confident insider explaining \
to smart friends" beats "formality: conversational."

**Characteristic phrases.** Exact recurring expressions, constructions, \
verbal tics, signature openers and transitions worth preserving.

**Anti-patterns.** Things this writer avoids or would reject.

**Hard editing rules.** If the sample contains prescriptive rules \
(before/after examples, "words to watch" lists, explicit do/don't rules), \
extract them faithfully at their stated severity. If a reference says \
"hard constraint, not a preference," preserve that.

{source_type_hint}

## Sample

Read the writing sample from: {source_path}
"""

SOURCE_TYPE_HINTS = {
    "conversation": (
        "## Reading this sample\n\n"
        "This is a conversation with <user>/<claude> tags. Analyze only "
        "the author's voice (<user> blocks). Distinguish between their "
        "instruction voice (how they direct the AI) and their target prose "
        "voice (what the finished piece should sound like). Correction pairs "
        '("don\'t write X, write Y") are direct evidence.'
    ),
    "prose": (
        "## Reading this sample\n\n"
        "This is a prose sample — treat as the author's own writing."
    ),
    "prescriptive": (
        "## Reading this sample\n\n"
        "This may contain prescriptive editing rules (before/after examples, "
        "do/don't lists). Extract the rules faithfully — these aren't voice "
        "descriptions, they're editing instructions."
    ),
    "format_reference": (
        "## Reading this sample\n\n"
        "This is an exemplary published piece in the target output format. "
        "Analyze the structural and stylistic conventions of the format "
        "itself — section patterns, heading style, tone register, pacing, "
        "how arguments are introduced, use of footnotes/asides/epistemic "
        "status markers. This is NOT the author's own voice — it's a model "
        "of what good output looks like in this format."
    ),
}


def source_type_hint(source_type: str) -> str:
    """How the analyst is told to read this kind of source, if it is told at all.

    Source kinds are names callers coin, so one without a hint is ordinary:
    the analyst then reads the sample with no framing.
    """
    return SOURCE_TYPE_HINTS[source_type] if source_type in SOURCE_TYPE_HINTS else ""


async def analyze_single_source(
    sample: StyleSample,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> VoiceAnalysis:
    """Analyze a single sample into its voice profile.

    The sample's label is carried through so identity survives asyncio.gather
    and filtering — callers never need positional indexing to match results
    back to sources.
    """
    text = sample.text
    label = sample.label
    key = voice_cache_key(text)
    cache_path = voice_cache_dir() / f"{key}.md"
    if cache_path.exists():
        logger.debug("Voice cache hit: %s", label)
        return VoiceAnalysis.from_cached(label, cache_path.read_text(encoding="utf-8"))

    source_dir = voice_cache_dir() / "inputs"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / f"{key}_input.md"
    source_path.write_text(text, encoding="utf-8")

    hint = source_type_hint(sample.source_type)
    prompt = SINGLE_SOURCE_PROMPT.format(
        source_type_hint=hint,
        source_path=source_path,
    )

    task = f"{prompt}\n\nWrite your complete analysis to: {cache_path}"

    await query(
        task,
        model=stage_model("voice"),
        system_prompt="You are a writing style analyst.",
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=mcp_servers or {},
        allowed_tools=mcp_tool_names or [],
        prefix=f"[voice:{label}] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if cache_path.exists():
        return VoiceAnalysis.from_cached(label, cache_path.read_text(encoding="utf-8"))
    # An empty analysis is dropped by every caller, so this is the last place
    # that knows the difference between a sample with no voice in it and an
    # analyst that was refused the file it was told to write. Unsaid, the run
    # goes on to write the whole piece with no voice guide and nothing about
    # the author's voice is ever mentioned again.
    logger.error(
        "Voice analysis of %s produced nothing: %s was never written", label, cache_path
    )
    return VoiceAnalysis(label=label)


MERGE_ANALYSES_PROMPT = """\
You have {count} individual voice analyses from writing samples by the \
same author. Merge them into a single, comprehensive voice and style \
guide that section writers, editors, and reviewers will use to match the \
author's voice and enforce their style rules.

Write it as a markdown document. Structure it however the combined input \
calls for.

## Merging rules

- Where analyses agree, strengthen the signal with combined examples
- Where they conflict, note the variation — the author may use different \
registers in different contexts
- Preserve ALL hard editing rules from any source — these are non-negotiable \
and must be specific enough for a rewriter to enforce mechanically
- Characteristic phrases that appear across multiple analyses are \
high-confidence signals
- Anti-patterns from any source apply globally unless directly contradicted

{format_section}

## Individual Analyses

{analyses_text}
"""


async def merge_voice_analyses(
    analyses: list[VoiceAnalysis],
    output_path: Path,
    format_examples: list[VoiceAnalysis] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> str:
    """Merge individual voice analyses into a unified guide."""
    analyses_text = "\n\n---\n\n".join(
        f"### Source: {one.label}\n\n{one.analysis}" for one in analyses
    )

    format_section = ""
    if format_examples:
        examples_text = "\n\n---\n\n".join(
            f"### {one.label}\n\n{one.analysis}" for one in format_examples
        )
        format_section = (
            "## Format Reference Analyses\n\n"
            "The following are style analyses of exemplary writing in the "
            "target format. Incorporate format-specific conventions "
            "(structure, tone, pacing, section patterns) into the unified "
            "guide alongside the author's voice:\n\n"
            f"{examples_text}"
        )

    merge_input_path = voice_cache_dir() / "merge_input.md"
    merge_content = MERGE_ANALYSES_PROMPT.format(
        count=len(analyses),
        analyses_text=analyses_text,
        format_section=format_section,
    )
    merge_input_path.write_text(merge_content, encoding="utf-8")

    task = (
        f"Read the merge instructions and individual analyses from: {merge_input_path}\n\n"
        f"Write the unified voice guide to: {output_path}"
    )

    await query(
        task,
        model=stage_model("voice"),
        system_prompt="You are a writing style analyst producing a unified voice guide.",
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=mcp_servers or {},
        allowed_tools=mcp_tool_names or [],
        prefix="[voice:merge] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if output_path.exists():
        return output_path.read_text(encoding="utf-8")
    return ""


def speaker_tagged(text: str) -> bool:
    """Whether this text is a transcript carrying <user>/<claude> speaker tags.

    Only the claude.ai share-link extractor produces them. A file, a web page,
    a Google Doc, or a published chapter handed to ``revise`` arrives as
    untagged prose, and telling the analyst to read its <user> blocks sends it
    looking for turns the text does not have.
    """
    return "<user>" in text or "<claude>" in text


def source_type_of(text: str) -> str:
    """How the analyst should read a sample: an author's turns, or prose."""
    return "conversation" if speaker_tagged(text) else "prose"


def extract_author_text(conversation: str) -> str:
    """Reduce a tagged conversation to the author's own writing.

    The author's voice lives in the untagged prose (their own draft) and in
    their <user> turns; the assistant's <claude> turns are not their voice. So
    the claude blocks are dropped and everything else is kept. This preserves a
    document's prose even when it is concatenated with a feedback conversation
    that carries speaker tags — the failure mode where a whole draft was
    discarded and only a few stray instruction lines survived. Text with no
    speaker tags is returned unchanged.
    """
    if not speaker_tagged(conversation):
        return conversation

    def kept() -> Iterator[str]:
        """Everything but the assistant's turns, in the order it was written."""
        i = 0
        while i < len(conversation):
            claude_at = conversation.find("<claude>", i)
            user_at = conversation.find("<user>", i)
            starts = [p for p in (claude_at, user_at) if p != -1]
            if not starts:
                yield conversation[i:]
                return
            nxt = min(starts)
            if nxt > i:
                yield conversation[i:nxt]
            if nxt == claude_at:
                close = conversation.find("</claude>", nxt)
                i = close + len("</claude>") if close != -1 else len(conversation)
            else:
                body_start = nxt + len("<user>")
                close = conversation.find("</user>", body_start)
                body_end = close if close != -1 else len(conversation)
                yield conversation[body_start:body_end]
                i = body_end + len("</user>") if close != -1 else len(conversation)

    return "\n\n".join(part.strip() for part in kept() if part.strip())


def samples_to_analyze(
    text: str, corpus: list[StyleSample] | None
) -> list[StyleSample]:
    """The session's source plus the corpus, each labelled and typed for analysis.

    Every sample's type is read off the sample. The session's own source used to
    be declared a conversation whatever it was, which handed the analyst a
    transcript's reading instructions for a draft that carries no turns.
    """
    return [
        StyleSample(
            label="source",
            text=extract_author_text(text),
            source_type=source_type_of(text),
        ),
        *(
            StyleSample(
                label=sample.label,
                text=sample.text,
                source_type=source_type_of(sample.text),
            )
            for sample in corpus or []
        ),
    ]


async def analyze_format_examples(
    target_format: str,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> list[VoiceAnalysis]:
    """Analyze the reference examples held for a format, if it has any."""
    results = await asyncio.gather(
        *(
            analyze_single_source(
                StyleSample(
                    label=f"format:{target_format}/{sample.label}",
                    text=sample.text,
                    source_type="format_reference",
                ),
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
                mcp_servers=mcp_servers,
                mcp_tool_names=mcp_tool_names,
            )
            for sample in await load_format_examples(target_format)
        )
    )
    return [one for one in results if one.analysis]


async def do_analyze_voice(
    text: str,
    corpus_samples: list[StyleSample] | None = None,
    target_format: str | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> str | None:
    """Analyze writing samples and return a freeform voice guide in markdown.

    Each source is analyzed individually (with content-hash caching) then
    merged into a unified guide. Format-specific reference examples are
    included in the merge when target_format is specified.
    """
    results = await asyncio.gather(
        *(
            analyze_single_source(
                sample,
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
                mcp_servers=mcp_servers,
                mcp_tool_names=mcp_tool_names,
            )
            for sample in samples_to_analyze(text, corpus_samples)
        )
    )

    analyses = [one for one in results if one.analysis]
    if not analyses:
        return None

    format_analyses = (
        await analyze_format_examples(
            target_format,
            trace_logger=trace_logger,
            cost_accumulator=cost_accumulator,
            mcp_servers=mcp_servers,
            mcp_tool_names=mcp_tool_names,
        )
        if target_format
        else []
    )

    if len(analyses) == 1 and not format_analyses:
        return analyses[0].analysis

    output_path = voice_cache_dir() / "merged_guide.md"
    return await merge_voice_analyses(
        analyses,
        output_path,
        format_examples=format_analyses,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
        mcp_servers=mcp_servers,
        mcp_tool_names=mcp_tool_names,
    )


async def analyze_voice_individually(
    text: str,
    corpus_samples: list[StyleSample] | None = None,
    target_format: str | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> list[VoiceAnalysis]:
    """Analyze voice sources individually, without merging them.

    An analysis marked prescriptive is the caller's cue to save the raw
    source verbatim instead of what the analyst made of it.
    """
    results = await asyncio.gather(
        *(
            analyze_single_source(
                sample,
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
                mcp_servers=mcp_servers,
                mcp_tool_names=mcp_tool_names,
            )
            for sample in samples_to_analyze(text, corpus_samples)
        )
    )
    analyses = [one for one in results if one.analysis]

    if not target_format:
        return analyses

    return analyses + [
        VoiceAnalysis(label=one.label, analysis=one.analysis)
        for one in await analyze_format_examples(
            target_format,
            trace_logger=trace_logger,
            cost_accumulator=cost_accumulator,
            mcp_servers=mcp_servers,
            mcp_tool_names=mcp_tool_names,
        )
    ]


class AnalyzeVoiceInput(BaseModel):
    text: str = Field(
        description=(
            "Text to analyze — a conversation excerpt, draft, or writing sample. "
            "If the style corpus is available, it will be included automatically."
        )
    )
    target_format: str = Field(
        default="",
        description=(
            "Target output format (e.g. 'lesswrong', 'twitter', 'blog'). "
            "When set, format-specific reference examples are analyzed and "
            "included in the voice guide."
        ),
    )


class VoiceAnalysisOutput(BaseModel):
    guide: str = Field(description="Freeform voice and style guide in markdown")


@lup_tool(
    "Analyze writing samples and produce a voice and style guide. "
    "Call this after loading the style corpus and extracting the source "
    "conversation. Pass the author's text (conversation excerpts, past "
    "writing, editing checklists) and get back a markdown guide covering "
    "what makes this author distinctive, phrases to preserve, anti-patterns "
    "to avoid, and any hard editing rules from prescriptive references. "
    "Section writers and the rewriter use this to match voice and enforce style."
)
async def analyze_voice(params: AnalyzeVoiceInput) -> VoiceAnalysisOutput:
    guide = await do_analyze_voice(
        params.text,
        await load_style_corpus(),
        target_format=params.target_format or None,
    )
    if guide is None:
        raise ToolError("Voice analysis produced no output")
    return VoiceAnalysisOutput(guide=guide)


def add_style_reference(
    source: str,
    target_format: str | None = None,
    prescriptive: bool = False,
) -> str:
    """Add a file or URL to the style corpus. Returns a status message.

    When target_format is specified, the reference is added as a
    format-specific example instead of a general voice reference.

    When prescriptive is True, the document is stored in the
    prescriptive/ subdirectory and will be passed through verbatim
    to writers rather than analyzed for voice characteristics.
    """
    if target_format:
        return add_format_example(target_format, source)

    style_dir = Path(current_settings().style_corpus_path)
    if prescriptive:
        style_dir = style_dir / "prescriptive"
    style_dir.mkdir(parents=True, exist_ok=True)

    label = "prescriptive rules" if prescriptive else "style corpus"

    source_path = Path(source).expanduser()
    if source_path.exists():
        content = source_path.read_text(encoding="utf-8")
        dest = style_dir / source_path.name
        dest.write_text(content, encoding="utf-8")
        return f"Added {source_path.name} to {label} ({len(content)} chars)"

    if prescriptive:
        return "Prescriptive references must be local files, not URLs"

    refs_file = style_dir / "urls.txt"
    existing = refs_file.read_text(encoding="utf-8") if refs_file.exists() else ""
    if source in existing:
        return f"Already in corpus: {source}"

    with refs_file.open("a", encoding="utf-8") as f:
        f.write(source + "\n")
    return f"Added URL to style corpus: {source}"


class StyleEntry(BaseModel):
    kind: str = Field(description="'file' or 'url'")
    name: str = Field(description="Filename or URL")
    size: int = Field(default=0, description="Size in bytes (files only)")


def style_entries(directory: Path) -> list[StyleEntry]:
    """Every reference a style directory holds: its files, and its listed URLs."""
    if not directory.exists():
        return []

    def listed() -> Iterator[StyleEntry]:
        for path in text_files_in(directory):
            if path.name != "urls.txt":
                yield StyleEntry(kind="file", name=path.name, size=path.stat().st_size)
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                url = line.strip()
                if url:
                    yield StyleEntry(kind="url", name=url)

    return list(listed())


class StyleCorpusListing(BaseModel):
    """What the style corpus holds: voice references, and rule documents."""

    voice: list[StyleEntry] = Field(
        default_factory=list, description="Samples the pipeline learns a voice from"
    )
    prescriptive: list[StyleEntry] = Field(
        default_factory=list, description="Rule documents passed through verbatim"
    )


def list_style_references() -> StyleCorpusListing:
    """List all entries in the style corpus."""
    style_dir = Path(current_settings().style_corpus_path)
    return StyleCorpusListing(
        voice=style_entries(style_dir),
        prescriptive=style_entries(style_dir / "prescriptive"),
    )


VOICE_TOOLS = [load_corpus, analyze_voice]
