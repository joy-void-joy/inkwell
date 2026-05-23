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
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)


class VoiceProfile(BaseModel):
    """Structured profile of an author's writing voice."""

    formality: str = Field(
        description="Level: 'casual', 'conversational', 'professional', 'academic'"
    )
    sentence_rhythm: str = Field(
        description="Mix of short/medium/long sentences and variation patterns"
    )
    hedging_style: str = Field(
        description="How the author handles uncertainty (e.g. 'I think', 'probably', 'it seems')"
    )
    humor: str = Field(
        description="Humor style or 'none'. E.g. 'dry asides', 'self-deprecating', 'wordplay'"
    )
    technical_depth: str = Field(
        description="How deep they go technically: 'accessible', 'moderate', 'expert-level'"
    )
    characteristic_phrases: list[str] = Field(
        default_factory=list,
        description="Recurring phrases, verbal tics, or signature expressions",
    )
    paragraph_style: str = Field(
        description="Typical paragraph length and structure"
    )
    argumentation: str = Field(
        description="How they build arguments: 'bottom-up evidence', 'top-down thesis', 'exploratory', 'dialectical'"
    )
    summary: str = Field(
        description="2-3 sentence overall voice description a writer could use to imitate"
    )


class AnalyzeVoiceInput(BaseModel):
    text: str = Field(
        description="Text to analyze for voice characteristics (conversation excerpt, writing sample)"
    )
    include_corpus: bool = Field(
        default=True,
        description="Include style corpus references in the analysis",
    )


class AnalyzeVoiceOutput(BaseModel):
    profile: VoiceProfile = Field(description="Extracted voice profile")
    corpus_samples: list[str] = Field(
        default_factory=list,
        description="Excerpts from the style corpus (first 500 chars each)",
    )
    corpus_count: int = Field(
        default=0,
        description="Number of style corpus references available",
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


VOICE_TOOLS = [load_corpus]
