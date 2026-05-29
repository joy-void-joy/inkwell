"""Voice analysis and style corpus tools.

Analyzes the author's writing style from source conversations and
reference pieces. Produces a freeform markdown voice guide that section
writers and the rewriter use to match the author's voice.
"""

import logging
from pathlib import Path

import trafilatura
from pydantic import BaseModel, Field

from inkwell.agent.config import settings
from lup.client import CostAccumulator, query
from lup.trace import TraceLogger
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


class LoadCorpusInput(BaseModel):
    max_samples: int = Field(
        default=5, ge=1, le=20, description="Max style samples to return"
    )


class LoadCorpusOutput(BaseModel):
    samples: list[str] = Field(description="Style corpus text samples")
    sources: list[str] = Field(description="Source file names or URLs")
    count: int = Field(description="Total samples available")


def load_style_corpus(max_samples: int = 5) -> tuple[list[str], list[str]]:
    """Load text samples from the style corpus directory."""
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
            samples.append(content[:2000])
            sources.append(f.name)
        if len(samples) >= max_samples:
            break

    urls_file = style_dir / "urls.txt"
    if urls_file.exists() and len(samples) < max_samples:
        for line in urls_file.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url:
                continue
            cached = fetch_cached_url(style_dir, url)
            if cached:
                samples.append(cached[:2000])
                sources.append(url)
            if len(samples) >= max_samples:
                break

    return samples, sources


def fetch_cached_url(style_dir: Path, url: str) -> str | None:
    """Return cached text for a URL, or fetch and cache it."""
    import hashlib

    cache_dir = style_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)

    slug = hashlib.sha256(url.encode()).hexdigest()[:12]
    cache_path = cache_dir / f"{slug}.txt"

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    try:
        import httpx

        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        text = trafilatura.extract(resp.text) or ""
        if text:
            cache_path.write_text(text, encoding="utf-8")
            return text
    except Exception:
        logger.debug("Failed to fetch style corpus URL: %s", url)

    return None


@lup_tool(
    "Load the author's style corpus — writing samples they've provided "
    "as voice references. Returns text excerpts from local files and "
    "fetched URLs in config/style/. Use these samples alongside the "
    "conversation's voice notes to match the author's writing style. "
    "Call this early in the pipeline before section writing begins."
)
async def load_corpus(params: LoadCorpusInput) -> LoadCorpusOutput:
    samples, sources = load_style_corpus(params.max_samples)

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
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> str | None:
    """Analyze writing samples and return a freeform voice guide in markdown.

    Used by both the pipeline and the MCP tool wrapper.
    Extracts only author (<user>) blocks from tagged conversations.
    """
    author_text = extract_author_text(text)
    all_text = [author_text]
    if corpus_samples:
        all_text.extend(corpus_samples)
    samples_text = "\n\n---\n\n".join(all_text)

    collector = await query(
        VOICE_ANALYSIS_PROMPT.format(samples_text=samples_text[:8000]),
        model="claude-opus-4-6",
        system_prompt="You are a writing style analyst.",
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        prefix="[voice] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    return collector.text.strip() if collector.text else None


class AnalyzeVoiceInput(BaseModel):
    text: str = Field(
        description=(
            "Text to analyze — a conversation excerpt, draft, or writing sample. "
            "If the style corpus is available, it will be included automatically."
        )
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
    corpus_samples, _ = load_style_corpus(max_samples=3)
    guide = await do_analyze_voice(params.text, corpus_samples)
    if guide is None:
        raise ToolError("Voice analysis produced no output")
    return VoiceAnalysisOutput(guide=guide)


def add_style_reference(source: str) -> str:
    """Add a file or URL to the style corpus. Returns a status message."""
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


VOICE_TOOLS = [load_corpus, analyze_voice]
