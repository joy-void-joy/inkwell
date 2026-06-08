# claude: ignore
"""Content safety: prevent oversized tool results from hitting SDK truncation.

The Claude Agent SDK truncates MCP tool results at ~200K characters, writing
the overflow to a temp file under tool-results/. Agents try to Read that
file, which is also too large, creating an infinite redirect loop.

This module provides:

1. Automatic spill in the lup_tool wrapper — oversized string fields in
   tool results are written to disk and replaced with pointer strings
   before the result reaches the SDK.

2. A PreToolUse hook that injects a default limit on Read calls so agents
   never accidentally read a massive file without pagination.

3. Utilities for splitting large markdown files at heading boundaries.
"""

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CONTENT_SAFETY_THRESHOLD = 150_000
MAX_READABLE_SIZE = 180_000
DEFAULT_READ_LIMIT = 2000
PREVIEW_CHARS = 500

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

content_dir: Path | None = None


def configure_content_safety(
    directory: Path,
) -> None:
    """Set the directory for saving tool content files."""
    global content_dir  # noqa: PLW0603
    content_dir = directory
    directory.mkdir(parents=True, exist_ok=True)


class SavedContent(BaseModel):
    """Metadata returned when content is saved to disk.

    Every extraction/read tool returns this (or embeds its fields)
    so agents always get a consistent interface: path + metadata,
    never raw large strings.
    """

    path: str = Field(description="File path to the saved content")
    word_count: int = Field(description="Approximate word count")
    char_count: int = Field(description="Character count")
    preview: str = Field(description="First ~500 characters for quick inspection")


def save_content(
    tool_name: str,
    label: str,
    content: str,
    directory: Path | None = None,
    ext: str = ".md",
) -> SavedContent:
    """Write content to disk and return metadata.

    This is the core primitive for write-first content delivery.
    Tools call this instead of returning large strings inline.
    The agent gets path + word count + preview — never the raw content.
    """
    target = directory or content_dir
    if target is None:
        raise RuntimeError(
            "No content directory configured — call configure_content_safety() first"
        )
    target.mkdir(parents=True, exist_ok=True)

    slug = slugify_label(label)
    path = target / f"{tool_name}_{slug}{ext}"

    counter = 0
    while path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            break
        counter += 1
        path = target / f"{tool_name}_{slug}_{counter}{ext}"

    path.write_text(content, encoding="utf-8")
    word_count = len(content.split())
    preview = content[:PREVIEW_CHARS]
    if len(content) > PREVIEW_CHARS:
        preview += "…"

    return SavedContent(
        path=str(path),
        word_count=word_count,
        char_count=len(content),
        preview=preview,
    )


def slugify_label(label: str) -> str:
    """Turn a label into a readable filename slug.

    Strips URL protocol/domain prefixes, collapses special characters,
    and truncates to 60 characters.
    """
    slug = label
    slug = re.sub(r"^https?://", "", slug)
    slug = re.sub(r"^www\.", "", slug)
    parts = slug.split("/")
    if len(parts) > 1:
        parts = [p for p in parts if p]
        slug = "-".join(parts[-2:]) if len(parts) >= 2 else parts[0]
    slug = slug.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:60]


def spill_field(tool_name: str, label: str, content: str, directory: Path) -> str:
    """Write a single oversized string field to disk, return pointer string.

    Legacy wrapper around save_content — used by the lup_tool spill fallback.
    """
    saved = save_content(tool_name, label, content, directory)
    return (
        f"Content written to {saved.path} ({saved.word_count} words). "
        f"Use Read with offset/limit to access."
    )


def spill_oversized_result(
    tool_name: str,
    label: str,
    result: BaseModel,
    directory: Path,
) -> BaseModel:
    """Walk a BaseModel's fields and spill oversized strings to disk.

    Returns a copy of the model with large string fields replaced by
    pointer strings. Non-string fields and small strings are untouched.
    """
    updates: dict[str, Any] = {}

    for field_name, value in result:
        if not isinstance(value, str):
            continue
        if len(value) <= CONTENT_SAFETY_THRESHOLD:
            continue
        pointer = spill_field(tool_name, label, value, directory)
        updates[field_name] = pointer

    if not updates:
        return result

    logger.info(
        "Spilling %d field(s) from %s (label=%s)",
        len(updates),
        tool_name,
        label[:80],
    )
    return result.model_copy(update=updates)


def split_on_headings(content: str) -> list[tuple[str, str]]:
    """Split markdown content at ## and ### heading boundaries.

    Returns (heading, chunk_text) pairs. Content before the first
    heading gets heading "Preamble". Falls back to a single chunk
    if no headings are found.
    """
    lines = content.split("\n")
    matches: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) <= 3:
            matches.append((i, m.group(2).strip()))

    if not matches:
        return [("Full content", content)]

    chunks: list[tuple[str, str]] = []

    if matches[0][0] > 0:
        preamble = "\n".join(lines[: matches[0][0]])
        if preamble.strip():
            chunks.append(("Preamble", preamble))

    for idx, (line_offset, heading) in enumerate(matches):
        end = matches[idx + 1][0] if idx + 1 < len(matches) else len(lines)
        chunk_text = "\n".join(lines[line_offset:end])
        chunks.append((heading, chunk_text))

    return chunks


def ensure_readable(path: Path, directory: Path) -> list[Path]:
    """Split a file into chunks if it exceeds MAX_READABLE_SIZE.

    Returns a list of chunk paths. If the file is already small enough,
    returns a single-element list with the original path.
    """
    content = path.read_text(encoding="utf-8")
    if len(content) <= MAX_READABLE_SIZE:
        return [path]

    directory.mkdir(parents=True, exist_ok=True)
    chunks = split_on_headings(content)
    if len(chunks) <= 1:
        return [path]

    stem = path.stem
    suffix = path.suffix or ".md"
    paths: list[Path] = []
    for i, (_heading, chunk_text) in enumerate(chunks):
        chunk_path = directory / f"{stem}_{i}{suffix}"
        chunk_path.write_text(chunk_text, encoding="utf-8")
        paths.append(chunk_path)

    return paths
