"""Output format adapter tools.

Transform a generic article draft into format-specific output:
LessWrong, Twitter thread, or blog. Called by the rewriter after
producing the final draft.

Each format has a plain function (do_format_*) for direct use by the
pipeline, and a @lup_tool wrapper for MCP access by the interactive agent.
"""

import logging
import textwrap

from pydantic import BaseModel, Field

from lup.mcp import lup_tool

logger = logging.getLogger(__name__)


class FormatLesswrongInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    epistemic_status: str = Field(
        description="Epistemic status statement (e.g. 'fairly confident, based on...')"
    )
    crossrefs: list[str] = Field(
        default_factory=list,
        description="Related posts/articles to cross-reference",
    )


class FormatLesswrongOutput(BaseModel):
    content: str = Field(description="LessWrong-formatted article")
    word_count: int = Field(description="Word count")


class FormatTwitterInput(BaseModel):
    content: str = Field(description="Full article content to thread-ify")
    hook: str = Field(description="Opening hook for tweet 1 (grabs attention)")


class FormatTwitterOutput(BaseModel):
    tweets: list[str] = Field(description="Individual tweets in thread order")
    thread_count: int = Field(description="Number of tweets in thread")


class DialogTurn(BaseModel):
    speaker: str = Field(description="Name or role of the speaker (e.g. 'Alice', 'Skeptic')")
    text: str = Field(description="What the speaker says in this turn")


class FormatDialogInput(BaseModel):
    content: str = Field(description="Full article content to restructure as dialog")
    speakers: list[str] | None = Field(
        default=None,
        description="Named participants (e.g. ['Alice', 'Bob'] or ['Advocate', 'Skeptic']). If omitted, the agent chooses names fitting the content.",
    )
    preamble: str = Field(
        default="",
        description="Optional brief introduction before the dialog begins",
    )


class FormatDialogOutput(BaseModel):
    preamble: str = Field(description="Introduction before the dialog")
    turns: list[DialogTurn] = Field(description="The dialog as structured turns")
    compiled: str = Field(description="Rendered dialog as markdown text")


class FormatBlogInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    title: str = Field(description="Article title")


class FormatBlogOutput(BaseModel):
    content: str = Field(description="Blog-formatted article")
    meta_description: str = Field(description="SEO meta description (150-160 chars)")
    word_count: int = Field(description="Word count")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences (simple heuristic)."""
    import re  # claude: ignore

    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Pure formatting functions (called directly by the pipeline)
# ---------------------------------------------------------------------------


def do_format_lesswrong(params: FormatLesswrongInput) -> FormatLesswrongOutput:
    lines: list[str] = []

    lines.append(f"*Epistemic status: {params.epistemic_status}*")
    lines.append("")

    footnotes: list[str] = []
    content = params.content
    footnote_idx = 0

    for line in content.splitlines():
        processed = line
        while "(" in processed and "aside:" in processed.lower():
            start = processed.find("(")
            end = processed.find(")", start)
            if end == -1:
                break
            aside_text = processed[start + 1 : end]
            if aside_text.lower().startswith("aside:"):
                footnote_idx += 1
                aside_content = aside_text[6:].strip()
                footnotes.append(f"[^{footnote_idx}]: {aside_content}")
                processed = (
                    processed[:start] + f"[^{footnote_idx}]" + processed[end + 1 :]
                )
            else:
                break
        lines.append(processed)

    if params.crossrefs:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("**Related:**")
        for ref in params.crossrefs:
            lines.append(f"- {ref}")

    if footnotes:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.extend(footnotes)

    result = "\n".join(lines)

    return FormatLesswrongOutput(
        content=result,
        word_count=len(result.split()),
    )


def do_format_twitter(params: FormatTwitterInput) -> FormatTwitterOutput:
    tweets: list[str] = []

    tweets.append(params.hook.strip())

    paragraphs = [p.strip() for p in params.content.split("\n\n") if p.strip()]

    for para in paragraphs:
        para = para.lstrip("#").strip()

        if len(para) <= 260:
            tweets.append(para)
        else:
            sentences = split_sentences(para)
            current = ""
            for sentence in sentences:
                candidate = f"{current} {sentence}".strip() if current else sentence
                if len(candidate) <= 260:
                    current = candidate
                else:
                    if current:
                        tweets.append(current)
                    if len(sentence) <= 260:
                        current = sentence
                    else:
                        for chunk in textwrap.wrap(sentence, 260):
                            tweets.append(chunk)
                        current = ""
            if current:
                tweets.append(current)

    numbered = []
    total = len(tweets)
    for i, tweet in enumerate(tweets, 1):
        numbered.append(f"{tweet}\n\n{i}/{total}")

    return FormatTwitterOutput(
        tweets=numbered,
        thread_count=len(numbered),
    )


async def do_format_dialog(params: FormatDialogInput) -> FormatDialogOutput:
    """Structure content as a dialog between named speakers via LLM query."""
    from lup.client import query

    class DialogTurns(BaseModel):
        turns: list[DialogTurn] = Field(description="Dialog turns in order")

    speaker_directive = (
        f"Use these speakers: {', '.join(params.speakers)}"
        if params.speakers
        else "Choose 2-3 speaker names that fit the content (e.g. named characters, roles like 'Skeptic'/'Advocate', or domain-appropriate labels)"
    )
    result = await query(
        (
            f"Rewrite this content as a natural dialog.\n"
            f"{speaker_directive}\n\n"
            f"Each speaker should have a distinct perspective. Distribute the "
            f"content's arguments and insights across speakers naturally — one "
            f"might raise objections, another might provide evidence, etc.\n\n"
            f"<content>\n{params.content}\n</content>"
        ),
        output_type=DialogTurns,
        model="claude-opus-4-6",
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        system_prompt=(
            "You restructure articles into dialogs. Each turn should feel natural "
            "and conversational while preserving the substance of the original. "
            "Vary turn length — some responses are a sentence, others a paragraph."
        ),
    )
    turns = result.turns if result else []

    lines: list[str] = []
    if params.preamble:
        lines.append(f"*{params.preamble}*")
        lines.append("")

    for turn in turns:
        lines.append(f"**{turn.speaker}:** {turn.text}")
        lines.append("")

    compiled = "\n".join(lines).strip()

    return FormatDialogOutput(
        preamble=params.preamble,
        turns=turns,
        compiled=compiled,
    )


def do_format_blog(params: FormatBlogInput) -> FormatBlogOutput:
    lines: list[str] = []
    lines.append(f"# {params.title}")
    lines.append("")

    paragraphs = params.content.split("\n\n")

    for para in paragraphs:
        stripped = para.strip()
        if not stripped:
            continue

        if stripped.startswith("#"):
            lines.append(stripped)
            lines.append("")
            continue

        if len(stripped) > 800:
            mid = len(stripped) // 2
            break_point = stripped.rfind(". ", 0, mid)
            if break_point == -1:
                break_point = stripped.find(". ", mid)
            if break_point != -1:
                lines.append(stripped[: break_point + 1].strip())
                lines.append("")
                lines.append(stripped[break_point + 1 :].strip())
                lines.append("")
                continue

        lines.append(stripped)
        lines.append("")

    result = "\n".join(lines).strip()

    first_para = ""
    for para in paragraphs:
        stripped = para.strip()
        if stripped and not stripped.startswith("#"):
            first_para = stripped
            break
    meta = (
        first_para[:157].rsplit(" ", 1)[0] + "..."
        if len(first_para) > 160
        else first_para
    )

    return FormatBlogOutput(
        content=result,
        meta_description=meta,
        word_count=len(result.split()),
    )


# ---------------------------------------------------------------------------
# MCP tool wrappers (thin async wrappers for agent access)
# ---------------------------------------------------------------------------


@lup_tool(
    "Format an article for LessWrong publication. Adds epistemic status "
    "header, converts inline asides to footnotes, adds cross-references "
    "to related posts, and ensures heading depth is appropriate for LW. "
    "Call this after the final draft is complete."
)
async def format_lesswrong(params: FormatLesswrongInput) -> FormatLesswrongOutput:
    return do_format_lesswrong(params)


@lup_tool(
    "Convert an article into a Twitter/X thread. Splits content into "
    "individual tweets (280 char limit), adds thread numbering, ensures "
    "each tweet can stand alone while building on the thread. The hook "
    "parameter becomes tweet 1 — it should grab attention without "
    "needing context. Call this after the final draft is complete."
)
async def format_twitter(params: FormatTwitterInput) -> FormatTwitterOutput:
    return do_format_twitter(params)


@lup_tool(
    "Format an article for blog publication. Adds SEO-friendly structure "
    "with a meta description, ensures subheading density for scannability, "
    "and optimizes paragraph length. Call this after the final draft "
    "is complete."
)
async def format_blog(params: FormatBlogInput) -> FormatBlogOutput:
    return do_format_blog(params)


@lup_tool(
    "Restructure an article as a dialog between named speakers. Each speaker "
    "gets a distinct perspective — one might advocate, another might push back. "
    "Produces structured turns (speaker + text) and a compiled markdown rendering. "
    "Use this when the content is naturally dialectical (opposing views, Q&A, "
    "interview format, Socratic exploration). Call after the final draft is complete."
)
async def format_dialog(params: FormatDialogInput) -> FormatDialogOutput:
    return await do_format_dialog(params)


FORMAT_TOOLS = [format_lesswrong, format_twitter, format_blog, format_dialog]
