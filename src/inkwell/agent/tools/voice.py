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

import trafilatura
from pydantic import BaseModel, Field

from claude_agent_sdk import McpServerConfig

from inkwell.agent.config import settings
from lup.client import CostAccumulator, query
from lup.trace import TraceLogger
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob"]


class LoadCorpusInput(BaseModel):
    max_samples: int = Field(
        default=5, ge=1, le=20, description="Max style samples to return"
    )


class LoadCorpusOutput(BaseModel):
    samples: list[str] = Field(description="Style corpus text samples")
    sources: list[str] = Field(description="Source file names or URLs")
    count: int = Field(description="Total samples available")


def load_style_corpus() -> tuple[list[str], list[str]]:
    """Load all text samples from the style corpus directory.

    Returns the complete text of every sample — no truncation, no cap.
    Pipeline stages save the result as a file artifact and let agents
    Read it with the built-in tool.
    """
    style_dir = Path(settings.style_corpus_path)
    if not style_dir.exists():
        return [], []

    samples: list[str] = []
    sources: list[str] = []

    text_files = sorted(style_dir.glob("*.md")) + sorted(style_dir.glob("*.txt"))
    for f in text_files:
        if f.name == "urls.txt":
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            samples.append(content)
            sources.append(f.name)

    urls_file = style_dir / "urls.txt"
    if urls_file.exists():
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url:
                continue
            cached = fetch_cached_url(style_dir, url)
            if cached:
                samples.append(cached)
                sources.append(url)

    return samples, sources


def fetch_cached_url(cache_parent: Path, url: str) -> str | None:
    """Return cached text for a URL, or fetch and cache it."""
    cache_dir = cache_parent / ".cache"
    cache_dir.mkdir(exist_ok=True)

    slug = hashlib.sha256(url.encode()).hexdigest()[:12]
    cache_path = cache_dir / f"{slug}.txt"

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    try:
        import httpx

        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        text = trafilatura.extract(resp.text) or resp.text.strip()
        if text:
            cache_path.write_text(text, encoding="utf-8")
            return text
    except Exception:
        logger.debug("Failed to fetch style corpus URL: %s", url)

    return None


# ---------------------------------------------------------------------------
# Format-specific reference examples
# ---------------------------------------------------------------------------


def load_format_examples(target_format: str) -> tuple[list[str], list[str]]:
    """Load reference examples for a specific output format.

    Looks in config/style/formats/<format>/ for .md/.txt files and urls.txt.
    Returns (samples, sources) — empty if no examples exist for this format.
    """
    base_format = target_format.split(":")[0]
    format_dir = Path(settings.style_corpus_path) / "formats" / base_format
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
            cached = fetch_cached_url(format_dir, url)
            if cached:
                samples.append(cached)
                sources.append(url)

    return samples, sources


def add_format_example(target_format: str, source: str) -> str:
    """Add a file or URL as a format-specific reference example."""
    base_format = target_format.split(":")[0]
    format_dir = Path(settings.style_corpus_path) / "formats" / base_format
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


def list_format_examples(target_format: str | None = None) -> dict[str, list[StyleEntry]]:
    """List format-specific reference examples, optionally filtered to one format."""
    formats_dir = Path(settings.style_corpus_path) / "formats"
    if not formats_dir.exists():
        return {}

    result: dict[str, list[StyleEntry]] = {}
    dirs = [formats_dir / target_format.split(":")[0]] if target_format else sorted(formats_dir.iterdir())

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
                entries.append(StyleEntry(kind="file", name=f.name, size=f.stat().st_size))
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
    all_samples, all_sources = load_style_corpus()
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
    )


# ---------------------------------------------------------------------------
# Per-source voice analysis with caching
# ---------------------------------------------------------------------------


def compute_voice_fingerprint(
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
    fmt_samples, _ = load_format_examples(target_format) if target_format else ([], [])
    for sample in fmt_samples:
        h.update(sample.encode())
    return h.hexdigest()[:24]


def voice_cache_key(text: str) -> str:
    """Content-addressable cache key for a text source."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def voice_cache_dir() -> Path:
    """Cache directory for per-source voice analyses."""
    cache = Path(settings.style_corpus_path) / ".cache" / "voice"
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

{text}
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
        'do/don\'t lists). Extract the rules faithfully — these aren\'t voice '
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


async def analyze_single_source(
    text: str,
    label: str,
    source_type: str = "prose",
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerConfig] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> str:
    """Analyze a single source and return its voice profile. Uses cache."""
    key = voice_cache_key(text)
    cache_path = voice_cache_dir() / f"{key}.md"
    if cache_path.exists():
        logger.debug("Voice cache hit: %s", label)
        return cache_path.read_text(encoding="utf-8")

    hint = SOURCE_TYPE_HINTS.get(source_type, "")
    prompt = SINGLE_SOURCE_PROMPT.format(
        source_type_hint=hint,
        text=text[:48000],
    )

    task = (
        f"{prompt}\n\n"
        f"Write your complete analysis to: {cache_path}"
    )

    await query(
        task,
        model="claude-sonnet-4-20250514",
        system_prompt="You are a writing style analyst.",
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers or {},
        allowed_tools=mcp_tool_names or [],
        prefix=f"[voice:{label}] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    return ""


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
    mcp_servers: dict[str, McpServerConfig] | None = None,
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

    prompt = MERGE_ANALYSES_PROMPT.format(
        count=len(analyses),
        analyses_text=analyses_text,
        format_section=format_section,
    )

    task = (
        f"{prompt}\n\n"
        f"Write the unified voice guide to: {output_path}"
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt="You are a writing style analyst producing a unified voice guide.",
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers or {},
        allowed_tools=mcp_tool_names or [],
        prefix="[voice:merge] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if output_path.exists():
        return output_path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Kept for backwards-compat prompt reference in the MCP tool's original
# single-shot path (when there's exactly one source and no format examples).
# ---------------------------------------------------------------------------

VOICE_ANALYSIS_PROMPT = """\
Write a comprehensive voice and style guide for the author based on the \
material below. This guide will be read by section writers, editors, and \
reviewers who need to match the author's voice and follow their style rules.

Write it as a markdown document. Structure it however the input calls for — \
the sections below are suggestions, not a template. If a section doesn't \
apply, skip it. If the input warrants sections not listed here, add them.

## What to cover (when relevant)

**Voice.** What makes this author sound like *them*, not a generic writer. \
Tone, rhythm, sentence structure, formality, humor, hedging patterns, \
argumentation style, paragraph construction. Be evocative and use examples \
from the samples — "writes like a confident insider explaining to smart \
friends" beats "formality: conversational."

**Characteristic phrases.** Exact recurring expressions, constructions, \
verbal tics, signature openers and transitions worth preserving.

**Anti-patterns.** Things this author never does or would reject.

**Hard editing rules.** Some samples may be prescriptive — editing \
checklists, style guides, or "humanizer" rules with before/after examples \
and concrete mechanical rules ("no em dashes," "replace not-X-but-Y"). \
When you encounter prescriptive input, extract the rules at their stated \
severity. If a reference says "hard constraint, not a preference," the \
guide must say the same. These rules are enforced by the rewriter, so be \
specific and unambiguous about what's banned and what to replace it with.

## How to read the samples

The samples may include a mix of the author's own writing, conversations, \
and prescriptive references. Treat each on its own terms:

- **Conversations** (with <user>/<claude> tags): analyze only the author's \
voice (<user> blocks). Distinguish between their instruction voice (how \
they direct the AI) and their target prose voice (what the finished piece \
should sound like). When they provide correction pairs ("don't write X, \
write Y"), those are direct evidence.
- **Prose samples** (no tags): treat as the author's own writing.
- **Prescriptive references** (contain before/after examples, "words to \
watch" lists, explicit do/don't rules): extract the rules faithfully. \
These aren't voice descriptions — they're editing instructions.

## Samples

{samples_text}
"""


def extract_author_text(conversation: str) -> str:
    """Extract only the author's (<user>) blocks from a tagged conversation.

    If the text has no <user>/<claude> tags, returns it unchanged.
    """
    if "<user>" not in conversation:
        return conversation
    blocks: list[str] = []
    remaining = conversation
    while "<user>" in remaining:
        start = remaining.index("<user>") + len("<user>")
        end_tag = "</user>"
        end = remaining.index(end_tag, start) if end_tag in remaining[start:] else len(remaining)
        block = remaining[start:end].strip()
        if block:
            blocks.append(block)
        remaining = remaining[end + len(end_tag):] if end < len(remaining) else ""
    return "\n\n---\n\n".join(blocks) if blocks else conversation


async def do_analyze_voice(
    text: str,
    corpus_samples: list[str] | None = None,
    corpus_sources: list[str] | None = None,
    target_format: str | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    mcp_servers: dict[str, McpServerConfig] | None = None,
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

    results = await asyncio.gather(*(
        analyze_single_source(
            source_text, label, stype,
            trace_logger=trace_logger,
            cost_accumulator=cost_accumulator,
            mcp_servers=mcp_servers,
            mcp_tool_names=mcp_tool_names,
        )
        for label, source_text, stype in sources_to_analyze
    ))

    analyses = [
        (sources_to_analyze[i][0], result)
        for i, result in enumerate(results)
        if result
    ]
    if not analyses:
        return None

    format_analyses: list[tuple[str, str]] | None = None
    if target_format:
        fmt_samples, fmt_sources = load_format_examples(target_format)
        if fmt_samples:
            fmt_results = await asyncio.gather(*(
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
            ))
            format_analyses = [
                (fmt_sources[i], result)
                for i, result in enumerate(fmt_results)
                if result
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
    mcp_servers: dict[str, McpServerConfig] | None = None,
    mcp_tool_names: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Analyze voice sources individually and return (label, analysis_text) pairs.

    Unlike do_analyze_voice, this skips the merge step. Downstream stages
    receive individual analysis files they can each Read without truncation.
    """
    author_text = extract_author_text(text)

    sources_to_analyze: list[tuple[str, str, str]] = [
        ("conversation", author_text, "conversation"),
    ]
    if corpus_samples:
        labels = corpus_sources or [f"sample-{i}" for i in range(len(corpus_samples))]
        for sample, label in zip(corpus_samples, labels):
            source_type = "conversation" if "<user>" in sample else "prescriptive"
            sources_to_analyze.append((label, sample, source_type))

    results = await asyncio.gather(*(
        analyze_single_source(
            source_text, label, stype,
            trace_logger=trace_logger,
            cost_accumulator=cost_accumulator,
            mcp_servers=mcp_servers,
            mcp_tool_names=mcp_tool_names,
        )
        for label, source_text, stype in sources_to_analyze
    ))

    analyses: list[tuple[str, str]] = [
        (sources_to_analyze[i][0], result)
        for i, result in enumerate(results)
        if result
    ]

    if target_format:
        fmt_samples, fmt_sources = load_format_examples(target_format)
        if fmt_samples:
            fmt_results = await asyncio.gather(*(
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
            ))
            for i, result in enumerate(fmt_results):
                if result:
                    analyses.append((f"format:{fmt_sources[i]}", result))

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
    corpus_samples, corpus_sources = load_style_corpus()
    guide = await do_analyze_voice(
        params.text, corpus_samples, corpus_sources=corpus_sources,
        target_format=params.target_format or None,
    )
    if guide is None:
        raise ToolError("Voice analysis produced no output")
    return VoiceAnalysisOutput(guide=guide)


def add_style_reference(source: str, target_format: str | None = None) -> str:
    """Add a file or URL to the style corpus. Returns a status message.

    When target_format is specified, the reference is added as a
    format-specific example instead of a general voice reference.
    """
    if target_format:
        return add_format_example(target_format, source)

    style_dir = Path(settings.style_corpus_path)
    style_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source).expanduser()
    if source_path.exists():
        content = source_path.read_text(encoding="utf-8")
        dest = style_dir / source_path.name
        dest.write_text(content, encoding="utf-8")
        return f"Added {source_path.name} to style corpus ({len(content)} chars)"

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


def list_style_references() -> list[StyleEntry]:
    """List all entries in the style corpus."""
    style_dir = Path(settings.style_corpus_path)
    if not style_dir.exists():
        return []

    entries: list[StyleEntry] = []
    files = sorted(style_dir.glob("*.md")) + sorted(style_dir.glob("*.txt"))
    for f in files:
        if f.name == "urls.txt":
            for line in f.read_text(encoding="utf-8").splitlines():
                url = line.strip()
                if url:
                    entries.append(StyleEntry(kind="url", name=url))
        else:
            entries.append(StyleEntry(kind="file", name=f.name, size=f.stat().st_size))
    return entries


EM_DASH = "—"
EN_DASH = "–"

VAGUE_ATTRIBUTIONS = [
    "experts say", "experts argue", "many believe", "some argue",
    "it is widely acknowledged", "it is well known", "studies show",
    "research shows", "according to experts",
]

DELIMITERS = set("*-•,\"'`()[]{}/ \t;:")


class VoiceViolation(BaseModel):
    rule: str = Field(description="Which rule was violated")
    text: str = Field(description="The violating text")
    line: int = Field(description="Line number (1-indexed)")


class VoiceViolationReport(BaseModel):
    violations: list[VoiceViolation] = Field(default_factory=list)
    passed: bool = Field(default=True)


def tokenize_words(line: str) -> list[str]:
    """Split a line into words by common delimiters."""
    buf: list[str] = []
    current: list[str] = []
    for ch in line:
        if ch in DELIMITERS:
            if current:
                buf.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        buf.append("".join(current))
    return buf


def extract_banned_words(voice_text: str) -> list[str]:
    """Parse banned vocabulary from voice analysis text."""
    banned: list[str] = []
    in_section = False
    for line in voice_text.splitlines():
        lower = line.lower().strip()
        if "banned" in lower and ("vocabulary" in lower or "words" in lower):
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("#") or (not line.strip() and banned):
                break
            words = tokenize_words(line)
            banned.extend(w.lower() for w in words if len(w) > 2)
    return banned


def verify_voice_rules(
    draft: str,
    voice_texts: list[str],
) -> VoiceViolationReport:
    """Check a draft against hard voice rules. Pure Python, no LLM call."""
    violations: list[VoiceViolation] = []
    lines = draft.splitlines()

    banned_words: list[str] = []
    for vt in voice_texts:
        banned_words.extend(extract_banned_words(vt))
    banned_words = list(set(banned_words))

    for i, line in enumerate(lines, 1):
        if EM_DASH in line:
            violations.append(VoiceViolation(
                rule="no em dashes", text=line.strip()[:120], line=i,
            ))
        if EN_DASH in line and not any(c.isdigit() for c in line):
            violations.append(VoiceViolation(
                rule="no en dashes (except number ranges)", text=line.strip()[:120], line=i,
            ))

    for i, line in enumerate(lines, 1):
        lower = line.lower()
        for word in banned_words:
            if word in lower:
                violations.append(VoiceViolation(
                    rule=f"banned vocabulary: {word}", text=line.strip()[:120], line=i,
                ))

    for i, line in enumerate(lines, 1):
        lower = line.lower()
        for phrase in VAGUE_ATTRIBUTIONS:
            if phrase in lower:
                violations.append(VoiceViolation(
                    rule="vague attribution", text=line.strip()[:120], line=i,
                ))

    return VoiceViolationReport(
        violations=violations,
        passed=len(violations) == 0,
    )


VOICE_TOOLS = [load_corpus, analyze_voice]
