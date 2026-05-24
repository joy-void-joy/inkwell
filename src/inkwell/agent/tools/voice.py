"""Voice analysis and style corpus tools.

Analyzes the author's writing style from source conversations and
reference pieces. Produces a structured VoiceProfile that section
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


class VoiceProfile(BaseModel):
    """Freeform profile of an author's writing voice."""

    voice: str = Field(
        description=(
            "Extensive freeform voice analysis. Write several paragraphs covering "
            "everything distinctive about this author's writing: tone, rhythm, "
            "sentence structure, formality, humor, hedging patterns, argumentation "
            "style, paragraph construction, technical depth, how they open and close "
            "pieces, how they handle uncertainty, what makes them sound like *them* "
            "and not a generic writer. Be specific and evocative — a writer should "
            "be able to read this and produce convincing imitation."
        )
    )
    phrases: list[str] = Field(
        default_factory=list,
        description=(
            "Exact phrases, constructions, and verbal tics to preserve or reuse. "
            "Include recurring expressions, signature sentence starters, "
            "characteristic transitions, and any phrases the author has explicitly "
            "requested be used."
        ),
    )
    avoid: list[str] = Field(
        default_factory=list,
        description=(
            "Anti-patterns — things this author never does or would reject. "
            "E.g. 'never uses exclamation marks', 'avoids listicle format', "
            "'no corporate jargon', 'doesn't hedge with \"I think\"'."
        ),
    )


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
Analyze the writing samples below and produce a voice profile.

Focus on what makes this author's voice distinctive — not generic writing \
observations. A ghostwriter should be able to read your analysis and produce \
convincing imitation.

In the `voice` field, write several detailed paragraphs. Cover tone, rhythm, \
sentence structure, formality, humor, hedging patterns, argumentation style, \
paragraph construction, technical depth — everything that makes this person \
sound like *them*. Use specific examples from the samples. Be evocative, not \
taxonomic — "writes like a confident insider explaining to smart friends" is \
better than "formality: conversational."

In `phrases`, extract exact recurring expressions, constructions, and verbal \
tics. Include signature sentence starters, characteristic transitions, and any \
phrases that feel load-bearing for the voice.

In `avoid`, note things this author clearly never does or would reject.

## Samples

{samples_text}
"""


async def do_analyze_voice(
    text: str,
    corpus_samples: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> VoiceProfile | None:
    """Analyze writing samples and return a structured voice profile.

    Used by both the pipeline and the MCP tool wrapper.
    """
    all_text = [text]
    if corpus_samples:
        all_text.extend(corpus_samples)
    samples_text = "\n\n---\n\n".join(all_text)

    result = await query(
        VOICE_ANALYSIS_PROMPT.format(samples_text=samples_text[:8000]),
        model="claude-opus-4-6",
        system_prompt="You are a writing style analyst.",
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        output_type=VoiceProfile,
        prefix="[voice] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    return result


class AnalyzeVoiceInput(BaseModel):
    text: str = Field(
        description=(
            "Text to analyze — a conversation excerpt, draft, or writing sample. "
            "If the style corpus is available, it will be included automatically."
        )
    )


@lup_tool(
    "Analyze writing samples and produce a freeform voice profile. "
    "Call this after loading the style corpus and extracting the source "
    "conversation. Pass the author's text (conversation excerpts, past "
    "writing) and get back a detailed voice analysis: extensive freeform "
    "notes on what makes this author distinctive, extracted phrases and "
    "verbal tics to preserve, and anti-patterns to avoid. Section writers "
    "use this to match voice."
)
async def analyze_voice(params: AnalyzeVoiceInput) -> VoiceProfile:
    corpus_samples, _ = load_style_corpus(max_samples=3)
    profile = await do_analyze_voice(params.text, corpus_samples)
    if profile is None:
        raise ToolError("Voice analysis failed to produce structured output")
    return profile


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
