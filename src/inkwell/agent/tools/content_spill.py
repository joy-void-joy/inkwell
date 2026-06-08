# claude: ignore
"""Content spill: write oversized tool content to files.

When a tool's content field exceeds the threshold, the MCP wrapper
writes full content to a spill file and returns a summary + file path.
The agent reads the full content via the Read tool when needed.

For structured content with markdown headings, spill_chunked splits
at section boundaries so the agent can read specific sections instead
of loading the entire file.
"""

import hashlib
from pathlib import Path

from inkwell.agent.content import ContentEnvelope, build_envelope

SPILL_THRESHOLD = 20_000
SPILL_DIR = Path("notes/spill")


def should_spill(content: str) -> bool:
    """Return True if content exceeds the spill threshold."""
    return len(content) > SPILL_THRESHOLD


def spill(tool_name: str, label: str, content: str, ext: str = "md") -> Path:
    """Write content to a spill file and return the path.

    Files are content-addressed by label, so repeated calls with the
    same label overwrite rather than accumulate.
    """
    SPILL_DIR.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha256(label.encode()).hexdigest()[:12]
    path = SPILL_DIR / f"{tool_name}_{slug}.{ext}"
    path.write_text(content, encoding="utf-8")
    return path


def spill_summary(content: str, max_preview: int = 500) -> str:
    """Return a short summary with word count and truncated preview."""
    word_count = len(content.split())
    preview = content[:max_preview]
    if len(content) > max_preview:
        preview += "..."
    return f"[{word_count} words]\n{preview}"


def spill_instruction(path: Path, word_count: int) -> str:
    """Return the instruction string that replaces content in the tool result."""
    return (
        f"Content written to {path} ({word_count} words). "
        f"Use the Read tool to access full content."
    )


def split_on_headings(content: str) -> list[tuple[str, str]]:
    """Split markdown content at ## and ### heading boundaries.

    Returns (heading, chunk_text) pairs. Content before the first
    heading gets heading "Preamble". Falls back to a single chunk
    if no headings are found.
    """
    from inkwell.agent.content import extract_sections

    sections = extract_sections(content)
    if not sections:
        return [("Full content", content)]

    lines = content.split("\n")
    chunks: list[tuple[str, str]] = []

    if sections[0].offset > 0:
        preamble = "\n".join(lines[: sections[0].offset])
        if preamble.strip():
            chunks.append(("Preamble", preamble))

    for i, section in enumerate(sections):
        end = sections[i + 1].offset if i + 1 < len(sections) else len(lines)
        chunk_text = "\n".join(lines[section.offset : end])
        chunks.append((section.heading, chunk_text))

    return chunks


def spill_chunked(
    tool_name: str,
    label: str,
    content: str,
    stage: str = "tool",
    content_type: str = "spill",
) -> ContentEnvelope:
    """Spill large content as section-aware chunks.

    Splits at markdown heading boundaries when possible, falling back
    to a single file for unstructured text. Returns a ContentEnvelope
    with extra_paths for overflow chunks.
    """
    SPILL_DIR.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha256(label.encode()).hexdigest()[:12]

    chunks = split_on_headings(content)

    if len(chunks) <= 1:
        path = SPILL_DIR / f"{tool_name}_{slug}.md"
        path.write_text(content, encoding="utf-8")
        return build_envelope(
            label=label,
            stage=stage,
            content_type=content_type,
            path=path,
            content=content,
        )

    paths: list[Path] = []
    for i, (_heading, chunk_text) in enumerate(chunks):
        chunk_path = SPILL_DIR / f"{tool_name}_{slug}_{i}.md"
        chunk_path.write_text(chunk_text, encoding="utf-8")
        paths.append(chunk_path)

    envelope = build_envelope(
        label=label,
        stage=stage,
        content_type=content_type,
        path=str(paths[0]),
        content=content,
        extra_paths=[str(p) for p in paths[1:]],
    )
    return envelope


def chunked_spill_instruction(envelope: ContentEnvelope) -> str:
    """Build a navigation-rich instruction string from a chunked spill envelope."""
    all_paths = [envelope.path] + envelope.extra_paths

    if not envelope.sections or len(all_paths) <= 1:
        return (
            f"Content written to {envelope.path} ({envelope.word_count} words). "
            f"Use the Read tool to access full content."
        )

    lines = [f"Content ({envelope.word_count} words) split into {len(all_paths)} parts:"]
    section_idx = 0
    for i, path in enumerate(all_paths):
        headings: list[str] = []
        while section_idx < len(envelope.sections):
            section = envelope.sections[section_idx]
            headings.append(f"{section.heading} ({section.word_count}w)")
            section_idx += 1
            if section_idx < len(envelope.sections) and i + 1 < len(all_paths):
                break
        summary = ", ".join(headings) if headings else f"Part {i + 1}"
        lines.append(f"  {path} — {summary}")

    lines.append("Use Read with offset/limit to navigate, or read a specific chunk file.")
    return "\n".join(lines)
