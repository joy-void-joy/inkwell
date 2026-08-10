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
from pathlib import Path

from pydantic import BaseModel, Field

from lup.mcp import McpServerEntry
from lup.runtime.usage import CostAccumulator

from inkwell.agent.config import current_settings, stage_model
from inkwell.agent.client import query
from lup.telemetry.trace import TraceLogger
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Agent"]


class LoadCorpusInput(BaseModel):
    max_samples: int = Field(
        default=5, ge=1, le=20, description="Max style samples to return"
    )


class LoadCorpusOutput(BaseModel):
    samples: list[str] = Field(description="Style corpus text samples")
    sources: list[str] = Field(description="Source file names or URLs")
    count: int = Field(description="Number of samples returned")
    total_available: int = Field(description="Total samples in the corpus")


async def load_style_corpus() -> tuple[list[str], list[str], list[str]]:
    """Load all text samples from the style corpus directory.

    Returns (samples, sources, source_types). Source types are "prose"
    for regular voice references and "prescriptive" for style guides,
    editing checklists, and other rule documents that should be passed
    through verbatim rather than analyzed for voice.

    Prescriptive documents live in config/style/prescriptive/.
    """
    style_dir = Path(current_settings().style_corpus_path)
    if not style_dir.exists():
        return [], [], []

    samples: list[str] = []
    sources: list[str] = []
    source_types: list[str] = []

    text_files = sorted(style_dir.glob("*.md")) + sorted(style_dir.glob("*.txt"))
    for f in text_files:
        if f.name == "urls.txt":
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            samples.append(content)
            sources.append(f.name)
            source_types.append("prose")

    urls_file = style_dir / "urls.txt"
    if urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url:
                continue
            cached = await fetch_cached_url(style_dir, url)
            if cached:
                samples.append(cached)
                sources.append(url)
                source_types.append("prose")

    prescriptive_dir = style_dir / "prescriptive"
    if prescriptive_dir.exists():
        presc_files = sorted(prescriptive_dir.glob("*.md")) + sorted(
            prescriptive_dir.glob("*.txt")
        )
        for f in presc_files:
            content = f.read_text(encoding="utf-8").strip()
            if content:
                samples.append(content)
                sources.append(f"prescriptive/{f.name}")
                source_types.append("prescriptive")

    return samples, sources, source_types


async def fetch_cached_url(cache_parent: Path, url: str) -> str | None:
    """Return cached text for a URL, or fetch and cache it."""
    cache_dir = cache_parent / ".cache"
    cache_dir.mkdir(exist_ok=True)

    slug = hashlib.sha256(url.encode()).hexdigest()[:12]
    cache_path = cache_dir / f"{slug}.txt"

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    try:
        from pathlib import Path

        from inkwell.agent.tools.research.fetch import do_fetch_source

        result = await do_fetch_source(url)
        text = Path(result.content.path).read_text(encoding="utf-8")
        if text:
            cache_path.write_text(text, encoding="utf-8")
            return text
    except Exception:
        logger.debug("Failed to fetch style corpus URL: %s", url)

    return None


# ---------------------------------------------------------------------------
# Format-specific reference examples
# ---------------------------------------------------------------------------


async def load_format_examples(target_format: str) -> tuple[list[str], list[str]]:
    """Load reference examples for a specific output format.

    Looks in config/style/formats/<format>/ for .md/.txt files and urls.txt.
    Returns (samples, sources) — empty if no examples exist for this format.
    """
    base_format = target_format.split(":")[0]
    format_dir = Path(current_settings().style_corpus_path) / "formats" / base_format
    if not format_dir.exists():
        return [], []

    samples: list[str] = []
    sources: list[str] = []

    text_files = sorted(format_dir.glob("*.md")) + sorted(format_dir.glob("*.txt"))
    for f in text_files:
        if f.name == "urls.txt":
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            samples.append(content)
            sources.append(f.name)

    urls_file = format_dir / "urls.txt"
    if urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url:
                continue
            cached = await fetch_cached_url(format_dir, url)
            if cached:
                samples.append(cached)
                sources.append(url)

    return samples, sources


def add_format_example(target_format: str, source: str) -> str:
    """Add a file or URL as a format-specific reference example."""
    base_format = target_format.split(":")[0]
    format_dir = Path(current_settings().style_corpus_path) / "formats" / base_format
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

    result: dict[str, list[StyleEntry]] = {}
    dirs = (
        [formats_dir / target_format.split(":")[0]]
        if target_format
        else sorted(formats_dir.iterdir())
    )

    for fmt_dir in dirs:
        if not fmt_dir.is_dir():
            continue
        entries: list[StyleEntry] = []
        files = sorted(fmt_dir.glob("*.md")) + sorted(fmt_dir.glob("*.txt"))
        for f in files:
            if f.name == "urls.txt":
                for line in f.read_text(encoding="utf-8").splitlines():
                    url = line.strip()
                    if url:
                        entries.append(StyleEntry(kind="url", name=url))
            else:
                entries.append(
                    StyleEntry(kind="file", name=f.name, size=f.stat().st_size)
                )
        if entries:
            result[fmt_dir.name] = entries

    return result


@lup_tool(
    "Load the author's style corpus — writing samples they've provided "
    "as voice references. Returns text excerpts from local files and "
    "fetched URLs in config/style/. Use these samples alongside the "
    "conversation's voice notes to match the author's writing style. "
    "Call this early in the pipeline before section writing begins."
)
async def load_corpus(params: LoadCorpusInput) -> LoadCorpusOutput:
    all_samples, all_sources, _types = await load_style_corpus()
    samples = all_samples[: params.max_samples]
    sources = all_sources[: params.max_samples]

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
    fmt_samples, _ = (
        (await load_format_examples(target_format)) if target_format else ([], [])
    )
    for sample in fmt_samples:
        h.update(sample.encode())
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


def parse_prescriptive_flag(text: str) -> tuple[str, bool]:
    """Parse YAML frontmatter for the prescriptive classification flag.

    Returns (body_text, is_prescriptive). Old cached analyses without
    frontmatter return (original_text, False).
    """
    stripped = text.strip()
    if not stripped.startswith("---"):
        return text, False
    end = stripped.find("---", 3)
    if end == -1:
        return text, False
    frontmatter = stripped[3:end]
    body = stripped[end + 3 :].strip()
    is_prescriptive = "prescriptive: true" in frontmatter.lower()
    return body or text, is_prescriptive


async def analyze_single_source(
    text: str,
    label: str,
    source_type: str = "prose",
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> tuple[str, str, bool]:
    """Analyze a single source and return (label, voice_profile, is_prescriptive).

    The label is passed through so identity survives asyncio.gather and
    filtering — callers never need positional indexing to match results
    back to sources.
    """
    key = voice_cache_key(text)
    cache_path = voice_cache_dir() / f"{key}.md"
    if cache_path.exists():
        logger.debug("Voice cache hit: %s", label)
        cached = cache_path.read_text(encoding="utf-8")
        analysis, is_prescriptive = parse_prescriptive_flag(cached)
        return label, analysis, is_prescriptive

    source_dir = voice_cache_dir() / "inputs"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / f"{key}_input.md"
    source_path.write_text(text, encoding="utf-8")

    hint = SOURCE_TYPE_HINTS.get(source_type, "")
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
        raw = cache_path.read_text(encoding="utf-8")
        analysis, is_prescriptive = parse_prescriptive_flag(raw)
        return label, analysis, is_prescriptive
    return label, "", False


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
    analyses: list[tuple[str, str]],
    output_path: Path,
    format_examples: list[tuple[str, str]] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> str:
    """Merge individual voice analyses into a unified guide."""
    analyses_text = "\n\n---\n\n".join(
        f"### Source: {label}\n\n{text}" for label, text in analyses
    )

    format_section = ""
    if format_examples:
        examples_text = "\n\n---\n\n".join(
            f"### {label}\n\n{text}" for label, text in format_examples
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
    if "<user>" not in conversation and "<claude>" not in conversation:
        return conversation
    kept: list[str] = []
    i = 0
    while i < len(conversation):
        claude_at = conversation.find("<claude>", i)
        user_at = conversation.find("<user>", i)
        starts = [p for p in (claude_at, user_at) if p != -1]
        if not starts:
            kept.append(conversation[i:])
            break
        nxt = min(starts)
        if nxt > i:
            kept.append(conversation[i:nxt])
        if nxt == claude_at:
            close = conversation.find("</claude>", nxt)
            i = close + len("</claude>") if close != -1 else len(conversation)
        else:
            body_start = nxt + len("<user>")
            close = conversation.find("</user>", body_start)
            body_end = close if close != -1 else len(conversation)
            kept.append(conversation[body_start:body_end])
            i = body_end + len("</user>") if close != -1 else len(conversation)
    return "\n\n".join(part.strip() for part in kept if part.strip())


async def do_analyze_voice(
    text: str,
    corpus_samples: list[str] | None = None,
    corpus_sources: list[str] | None = None,
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
    author_text = extract_author_text(text)

    sources_to_analyze: list[tuple[str, str, str]] = [
        ("conversation", author_text, "conversation"),
    ]
    if corpus_samples:
        labels = corpus_sources or [f"sample-{i}" for i in range(len(corpus_samples))]
        for sample, label in zip(corpus_samples, labels):
            source_type = "conversation" if "<user>" in sample else "prose"
            sources_to_analyze.append((label, sample, source_type))

    results = await asyncio.gather(
        *(
            analyze_single_source(
                source_text,
                label,
                stype,
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
                mcp_servers=mcp_servers,
                mcp_tool_names=mcp_tool_names,
            )
            for label, source_text, stype in sources_to_analyze
        )
    )

    analyses = [
        (label, analysis) for label, analysis, _is_prescriptive in results if analysis
    ]
    if not analyses:
        return None

    format_analyses: list[tuple[str, str]] | None = None
    if target_format:
        fmt_samples, fmt_sources = await load_format_examples(target_format)
        if fmt_samples:
            fmt_results = await asyncio.gather(
                *(
                    analyze_single_source(
                        sample,
                        f"format:{target_format}/{source}",
                        "format_reference",
                        trace_logger=trace_logger,
                        cost_accumulator=cost_accumulator,
                        mcp_servers=mcp_servers,
                        mcp_tool_names=mcp_tool_names,
                    )
                    for sample, source in zip(fmt_samples, fmt_sources)
                )
            )
            format_analyses = [
                (fmt_label, analysis)
                for fmt_label, analysis, _is_prescriptive in fmt_results
                if analysis
            ]

    if len(analyses) == 1 and not format_analyses:
        return analyses[0][1]

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
    corpus_samples: list[str] | None = None,
    corpus_sources: list[str] | None = None,
    target_format: str | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> list[tuple[str, str, bool]]:
    """Analyze voice sources individually.

    Returns (label, analysis_text, is_prescriptive) triples. When
    is_prescriptive is True, the caller should save the raw source
    verbatim instead of the analysis.
    """
    author_text = extract_author_text(text)

    sources_to_analyze: list[tuple[str, str, str]] = [
        ("conversation", author_text, "conversation"),
    ]
    if corpus_samples:
        labels = corpus_sources or [f"sample-{i}" for i in range(len(corpus_samples))]
        for sample, label in zip(corpus_samples, labels):
            source_type = "conversation" if "<user>" in sample else "prose"
            sources_to_analyze.append((label, sample, source_type))

    results = await asyncio.gather(
        *(
            analyze_single_source(
                source_text,
                label,
                stype,
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
                mcp_servers=mcp_servers,
                mcp_tool_names=mcp_tool_names,
            )
            for label, source_text, stype in sources_to_analyze
        )
    )

    analyses: list[tuple[str, str, bool]] = [
        (label, analysis, is_prescriptive)
        for label, analysis, is_prescriptive in results
        if analysis
    ]

    if target_format:
        fmt_samples, fmt_sources = await load_format_examples(target_format)
        if fmt_samples:
            fmt_results = await asyncio.gather(
                *(
                    analyze_single_source(
                        sample,
                        f"format:{target_format}/{source}",
                        "format_reference",
                        trace_logger=trace_logger,
                        cost_accumulator=cost_accumulator,
                        mcp_servers=mcp_servers,
                        mcp_tool_names=mcp_tool_names,
                    )
                    for sample, source in zip(fmt_samples, fmt_sources)
                )
            )
            for fmt_label, analysis, _is_prescriptive in fmt_results:
                if analysis:
                    analyses.append((fmt_label, analysis, False))

    return analyses


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
    corpus_samples, corpus_sources, _types = await load_style_corpus()
    guide = await do_analyze_voice(
        params.text,
        corpus_samples,
        corpus_sources=corpus_sources,
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


def list_style_references() -> tuple[list[StyleEntry], list[StyleEntry]]:
    """List all entries in the style corpus.

    Returns (voice_entries, prescriptive_entries).
    """
    style_dir = Path(current_settings().style_corpus_path)

    voice_entries: list[StyleEntry] = []
    if style_dir.exists():
        files = sorted(style_dir.glob("*.md")) + sorted(style_dir.glob("*.txt"))
        for f in files:
            if f.name == "urls.txt":
                for line in f.read_text(encoding="utf-8").splitlines():
                    url = line.strip()
                    if url:
                        voice_entries.append(StyleEntry(kind="url", name=url))
            else:
                voice_entries.append(
                    StyleEntry(kind="file", name=f.name, size=f.stat().st_size)
                )

    prescriptive_entries: list[StyleEntry] = []
    prescriptive_dir = style_dir / "prescriptive"
    if prescriptive_dir.exists():
        files = sorted(prescriptive_dir.glob("*.md")) + sorted(
            prescriptive_dir.glob("*.txt")
        )
        for f in files:
            prescriptive_entries.append(
                StyleEntry(kind="file", name=f.name, size=f.stat().st_size)
            )

    return voice_entries, prescriptive_entries


VOICE_TOOLS = [load_corpus, analyze_voice]
