"""Output format adapters.

Transform a generic article draft into format-specific output:
LessWrong, Twitter thread, memo, academic paper, newsletter, or custom.
Called by the pipeline's format stage via apply_format after the final
draft is produced.
"""

import logging
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from inkwell.agent.config import stage_model
from inkwell.agent.prose import markdown_blocks
from inkwell.agent.segmenter import reader

logger = logging.getLogger(__name__)

BUILTIN_READ_TOOLS = ["Read", "Grep", "Glob"]


def save_content_for_query(content: str, label: str) -> Path:
    """Save content to a temp file for LLM query input. Returns the file path."""
    path = Path(tempfile.mkdtemp()) / f"{label}_input.md"
    path.write_text(content, encoding="utf-8")
    return path


class FormatLesswrongInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    epistemic_status: str = Field(
        default="",
        description=(
            "Epistemic status statement (e.g. 'fairly confident, based on...'). "
            "Empty means the draft already carries its own epistemic framing — "
            "no header is added."
        ),
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
    hook: str = Field(
        default="",
        description=(
            "Opening hook for tweet 1 (grabs attention). Empty lets the "
            "thread writer derive one from the content."
        ),
    )


class FormatTwitterOutput(BaseModel):
    tweets: list[str] = Field(description="Individual tweets in thread order")
    thread_count: int = Field(description="Number of tweets in thread")


class DialogTurn(BaseModel):
    speaker: str = Field(
        description="Name or role of the speaker (e.g. 'Alice', 'Skeptic')"
    )
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


class FormatMemoInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    title: str = Field(description="Memo subject line")
    to: str = Field(
        default="",
        description="Recipient(s) of the memo (e.g. 'Senior Leadership', 'Policy Committee')",
    )
    from_field: str = Field(
        default="",
        description="Author/sender of the memo",
    )
    classification: str = Field(
        default="",
        description="Classification or sensitivity marking (e.g. 'CONFIDENTIAL', 'FOR INTERNAL USE')",
    )


class FormatMemoOutput(BaseModel):
    content: str = Field(description="Memo-formatted document")
    word_count: int = Field(description="Word count")


class FormatAcademicInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    title: str = Field(description="Paper title")
    authors: list[str] = Field(default_factory=list, description="Author names")
    abstract: str = Field(
        default="",
        description="Abstract text (generated from content if empty)",
    )
    keywords: list[str] = Field(
        default_factory=list, description="Keywords for indexing"
    )


class FormatAcademicOutput(BaseModel):
    content: str = Field(description="Academic-formatted paper")
    word_count: int = Field(description="Word count")


class FormatNewsletterInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    title: str = Field(description="Newsletter title / subject line")
    subtitle: str = Field(default="", description="Subtitle or tagline")
    cta: str = Field(
        default="",
        description="Call-to-action text (e.g. 'Subscribe for more')",
    )


class FormatNewsletterOutput(BaseModel):
    content: str = Field(description="Newsletter-formatted article")
    word_count: int = Field(description="Word count")


class FormatLinkedinInput(BaseModel):
    content: str = Field(description="Full article markdown content")
    title: str = Field(description="Post topic / headline")
    hashtags: list[str] = Field(
        default_factory=list,
        description="Hashtags to append (e.g. ['AI', 'Strategy']). Empty lets "
        "the writer pick a few fitting ones.",
    )


class FormatLinkedinOutput(BaseModel):
    content: str = Field(description="LinkedIn-formatted post")
    word_count: int = Field(description="Word count")


class FormatCustomInput(BaseModel):
    content: str = Field(description="Full article content to reformat")
    format_description: str = Field(
        description="Description of the desired output format — tone, structure, audience, conventions. Be specific: 'academic abstract with keywords' or 'diplomatic cable with numbered paragraphs and SUBJECT/REF headers'."
    )
    title: str = Field(default="", description="Title or subject line, if applicable")


class FormatCustomOutput(BaseModel):
    content: str = Field(description="Reformatted content")
    word_count: int = Field(description="Word count")


TWEET_CHARS = 260
"""Characters a single tweet may carry, leaving room for the n/total marker."""

BLOG_PARAGRAPH_CHARS = 800
"""Length past which a blog paragraph is halved, so the post stays scannable."""

NEWSLETTER_PARAGRAPH_SENTENCES = 3
"""Sentences past which a newsletter paragraph is halved, for email reading."""


def split_sentences(text: str) -> list[str]:
    """`text` as the sentences it is written in.

    Segmented rather than pattern-matched, so an abbreviation or a decimal
    does not end a sentence, and the pieces come back carrying whatever
    markdown they arrived with — which is what lets a formatter re-emit them.
    """
    return [sentence.text for sentence in reader().sentences(text)]


def halved_at_sentence(text: str) -> Iterator[str]:
    """`text` as two halves, cut at the sentence boundary nearest its middle.

    Yields the text unchanged when it runs to a single sentence and there is
    no boundary to cut on.
    """
    sentences = split_sentences(text)
    if len(sentences) < 2:
        yield text
        return

    def prefix(count: int) -> str:
        """The first `count` sentences, rejoined."""
        return " ".join(sentences[:count])

    middle = len(text) // 2
    cut = min(range(1, len(sentences)), key=lambda n: abs(len(prefix(n)) - middle))
    yield prefix(cut)
    yield ""
    yield " ".join(sentences[cut:])


# ---------------------------------------------------------------------------
# Pure formatting functions (called directly by the pipeline)
# ---------------------------------------------------------------------------


def do_format_lesswrong(params: FormatLesswrongInput) -> FormatLesswrongOutput:
    footnotes: list[str] = []  # lup: ignore[empty-collection] — the fold's other half

    def demote_asides(line: str) -> str:
        """Move this line's `(aside: …)` parentheticals into `footnotes`."""
        processed = line
        while "(" in processed and "aside:" in processed.lower():
            start = processed.find("(")
            end = processed.find(")", start)
            if end == -1:
                break
            aside_text = processed[start + 1 : end]
            if not aside_text.lower().startswith("aside:"):
                break
            marker = f"[^{len(footnotes) + 1}]"
            footnotes.append(f"{marker}: {aside_text[len('aside:') :].strip()}")
            processed = processed[:start] + marker + processed[end + 1 :]
        return processed

    body = [demote_asides(line) for line in params.content.splitlines()]

    def rendered() -> Iterator[str]:
        """The post: status line, body, related links, then the footnotes."""
        if params.epistemic_status:
            yield f"*Epistemic status: {params.epistemic_status}*"
            yield ""
        yield from body
        if params.crossrefs:
            yield from ("", "---", "", "**Related:**")
            yield from (f"- {ref}" for ref in params.crossrefs)
        if footnotes:
            yield from ("", "---", "")
            yield from footnotes

    result = "\n".join(rendered())

    return FormatLesswrongOutput(
        content=result,
        word_count=len(result.split()),
    )


def number_thread(tweets: list[str]) -> list[str]:
    """Append n/total markers to each tweet."""
    total = len(tweets)
    return [f"{tweet}\n\n{i}/{total}" for i, tweet in enumerate(tweets, 1)]


def split_thread(content: str, hook: str) -> list[str]:
    """Mechanically split content into <=260-char tweets at sentence boundaries.

    Fallback for when the LLM thread writer is unavailable — produces
    correct-length tweets but no editorial judgment about what makes
    each tweet stand alone.
    """
    blocks = markdown_blocks(content)
    opening = hook or (blocks[0].text if blocks else "") or "Thread:"

    def packed(paragraph: str) -> Iterator[str]:
        """One paragraph as tweets, broken at sentence ends where it can be."""
        current = ""
        for sentence in split_sentences(paragraph):
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) <= TWEET_CHARS:
                current = candidate
                continue
            if current:
                yield current
            if len(sentence) <= TWEET_CHARS:
                current = sentence
                continue
            yield from textwrap.wrap(sentence, TWEET_CHARS)
            current = ""
        if current:
            yield current

    def thread() -> Iterator[str]:
        """The hook, then each block of the draft cut to tweet length."""
        yield opening.strip()[:TWEET_CHARS]
        for block in blocks:
            if len(block.text) <= TWEET_CHARS:
                yield block.text
            else:
                yield from packed(block.text)

    return list(thread())


THREAD_WRITER_SYSTEM = """\
You rewrite articles as Twitter/X threads. This is a rewrite, not a
split: every tweet must be self-contained, land a complete point, and
make the reader want the next one.

Rules:
- Each tweet <= 260 characters (numbering is added separately)
- Tweet 1 is the hook — it must grab attention with zero context
- One idea per tweet; cut connective tissue that only works in prose
- Keep the author's voice, claims, and specific numbers exactly
- Preserve the argument's order and force; drop padding, not substance
- No headers, no markdown formatting, no "a thread 🧵" clichés"""


async def do_format_twitter(params: FormatTwitterInput) -> FormatTwitterOutput:
    """Rewrite content as a thread via LLM, with mechanical splitting as fallback."""
    from inkwell.agent.client import query

    class TwitterThread(BaseModel):
        tweets: list[str] = Field(
            description="Self-contained tweets in thread order, each <= 260 characters"
        )

    hook_directive = (
        f"Use this hook for tweet 1: {params.hook}"
        if params.hook
        else "Write tweet 1 as a hook derived from the content's strongest claim."
    )
    content_path = save_content_for_query(params.content, "twitter")
    result = await query(
        (
            f"Rewrite this article as a thread.\n"
            f"{hook_directive}\n\n"
            f"Read the article from: {content_path}"
        ),
        output_type=TwitterThread,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=THREAD_WRITER_SYSTEM,
    )

    tweets = [t.strip()[:260] for t in result.tweets if t.strip()] if result else []
    if not tweets:
        logger.warning("Thread writer produced no tweets — using mechanical split")
        tweets = split_thread(params.content, params.hook)

    numbered = number_thread(tweets)
    return FormatTwitterOutput(
        tweets=numbered,
        thread_count=len(numbered),
    )


async def do_format_dialog(params: FormatDialogInput) -> FormatDialogOutput:
    """Structure content as a dialog between named speakers via LLM query."""
    from inkwell.agent.client import query

    class DialogTurns(BaseModel):
        turns: list[DialogTurn] = Field(description="Dialog turns in order")

    speaker_directive = (
        f"Use these speakers: {', '.join(params.speakers)}"
        if params.speakers
        else "Choose 2-3 speaker names that fit the content (e.g. named characters, roles like 'Skeptic'/'Advocate', or domain-appropriate labels)"
    )
    content_path = save_content_for_query(params.content, "dialog")
    result = await query(
        (
            f"Rewrite this content as a natural dialog.\n"
            f"{speaker_directive}\n\n"
            f"Each speaker should have a distinct perspective. Distribute the "
            f"content's arguments and insights across speakers naturally — one "
            f"might raise objections, another might provide evidence, etc.\n\n"
            f"Read the content from: {content_path}"
        ),
        output_type=DialogTurns,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=(
            "You restructure articles into dialogs. Each turn should feel natural "
            "and conversational while preserving the substance of the original. "
            "Vary turn length — some responses are a sentence, others a paragraph."
        ),
    )
    turns = result.turns if result else []

    def rendered() -> Iterator[str]:
        """The dialog: its preamble, then a blank line under every turn."""
        if params.preamble:
            yield f"*{params.preamble}*"
            yield ""
        for turn in turns:
            yield f"**{turn.speaker}:** {turn.text}"
            yield ""

    compiled = "\n".join(rendered()).strip()

    return FormatDialogOutput(
        preamble=params.preamble,
        turns=turns,
        compiled=compiled,
    )


def do_format_blog(params: FormatBlogInput) -> FormatBlogOutput:
    blocks = markdown_blocks(params.content)

    def scannable(paragraph: str) -> Iterator[str]:
        """A long paragraph halved at the sentence end nearest its middle."""
        if len(paragraph) <= BLOG_PARAGRAPH_CHARS:
            yield paragraph
            return
        yield from halved_at_sentence(paragraph)

    def rendered() -> Iterator[str]:
        """The post under its title, each block followed by a blank line."""
        yield f"# {params.title}"
        yield ""
        for block in blocks:
            if block.is_heading:
                yield block.source
            else:
                yield from scannable(block.text)
            yield ""

    result = "\n".join(rendered()).strip()

    lead = next((block.text for block in blocks if not block.is_heading), "")
    meta = textwrap.shorten(lead, width=160, placeholder="...") if lead else ""

    return FormatBlogOutput(
        content=result,
        meta_description=meta,
        word_count=len(result.split()),
    )


async def do_format_memo(params: FormatMemoInput) -> FormatMemoOutput:
    import datetime

    from inkwell.agent.client import query

    header_lines: list[str] = []

    if params.classification:
        header_lines.append(f"**{params.classification.upper()}**")
        header_lines.append("")

    header_lines.append("---")
    header_lines.append("")
    if params.to:
        header_lines.append(f"**TO:** {params.to}")
    if params.from_field:
        header_lines.append(f"**FROM:** {params.from_field}")
    header_lines.append(f"**DATE:** {datetime.date.today().isoformat()}")
    header_lines.append(f"**SUBJECT:** {params.title}")
    header_lines.append("")
    header_lines.append("---")
    header_lines.append("")
    header_block = "\n".join(header_lines)

    class MemoContent(BaseModel):
        content: str = Field(description="The restructured memo body in markdown")

    content_path = save_content_for_query(params.content, "memo")
    result = await query(
        (
            f"Restructure this content into proper memo format.\n\n"
            f"Read the content from: {content_path}"
        ),
        output_type=MemoContent,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=(
            "You restructure article-style content into memo format. "
            "Apply these rules strictly:\n\n"
            "1. EXECUTIVE SUMMARY: The first section (2-3 paragraphs) must "
            "state the problem, the recommendation, and the stakes. A reader "
            "who stops here should understand the core argument.\n\n"
            "2. BOLD TOPIC SENTENCES: The first sentence of every paragraph "
            "in the body must be **bold**. A reader skimming only bold "
            "sentences should get the full argument.\n\n"
            "3. SECTION HEADINGS: Use descriptive headings that state "
            "conclusions, not topics. 'The Open-Weight Window Is Closing' "
            "not 'Background on Open Weights.'\n\n"
            "4. BULLET POINTS: Use bullets for any list of 3+ items and "
            "for all action items. Never bury action items in prose.\n\n"
            "5. CONCLUSION WITH ACTIONS: End with numbered concrete next "
            "steps (who, what, when).\n\n"
            "6. TONE: Diplomatic and professional. Direct but not aggressive. "
            "No dramatic escalation, no zingers, no journalistic scene-setting. "
            "State stakes plainly and let facts carry the weight.\n\n"
            "7. LENGTH: Cut ruthlessly. Every paragraph must earn its place. "
            "Remove any paragraph that repeats a point made elsewhere.\n\n"
            "Preserve all substantive content, data, and citations. "
            "Output only the restructured memo body in markdown."
        ),
    )
    body = result.content if result else params.content

    final = header_block + body

    if params.classification:
        final += f"\n\n---\n**{params.classification.upper()}**"

    return FormatMemoOutput(
        content=final,
        word_count=len(final.split()),
    )


async def do_format_academic(params: FormatAcademicInput) -> FormatAcademicOutput:
    """Format content as an academic paper via LLM restructuring."""
    from inkwell.agent.client import query

    class AcademicContent(BaseModel):
        abstract: str = Field(description="Paper abstract (150-300 words)")
        content: str = Field(description="Restructured paper body in markdown")

    author_line = ", ".join(params.authors) if params.authors else ""
    keyword_line = ", ".join(params.keywords) if params.keywords else ""

    content_path = save_content_for_query(params.content, "academic")
    result = await query(
        (
            f"Restructure this content into an academic paper.\n\n"
            f"Title: {params.title}\n"
            + (f"Authors: {author_line}\n" if author_line else "")
            + (f"Abstract hint: {params.abstract}\n" if params.abstract else "")
            + (f"Keywords: {keyword_line}\n" if keyword_line else "")
            + f"\nRead the content from: {content_path}"
        ),
        output_type=AcademicContent,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=(
            "You restructure article-style content into academic paper format.\n\n"
            "1. ABSTRACT: Write a 150-300 word abstract if none provided.\n"
            "2. NUMBERED SECTIONS: Use 1, 1.1, 1.2, 2, etc. for headings.\n"
            "3. REGISTER: Formal academic register. Hedge appropriately. "
            "Use passive voice where the field convention expects it.\n"
            "4. CLAIMS: Ground every claim in evidence. Flag speculation.\n"
            "5. End with a 'References' section header (leave empty — "
            "citations are added by a separate tool).\n\n"
            "Preserve all substantive content. Output the abstract and "
            "restructured body as markdown."
        ),
    )
    abstract = result.abstract if result else params.abstract
    body = result.content if result else params.content

    lines: list[str] = [f"# {params.title}", ""]
    if author_line:
        lines.extend([f"*{author_line}*", ""])
    lines.extend(["## Abstract", "", abstract, ""])
    if keyword_line:
        lines.extend([f"**Keywords:** {keyword_line}", ""])
    lines.extend(["---", "", body])

    final = "\n".join(lines)
    return FormatAcademicOutput(content=final, word_count=len(final.split()))


def do_format_newsletter(params: FormatNewsletterInput) -> FormatNewsletterOutput:
    """Format content as a newsletter / Substack-style email."""

    def emailable(paragraph: str) -> Iterator[str]:
        """A long paragraph halved, so no screenful of email runs unbroken."""
        sentences = split_sentences(paragraph)
        if len(sentences) <= NEWSLETTER_PARAGRAPH_SENTENCES:
            yield paragraph
            return
        mid = len(sentences) // 2
        yield " ".join(sentences[:mid])
        yield ""
        yield " ".join(sentences[mid:])

    def rendered() -> Iterator[str]:
        """The issue: masthead, each block spaced out, then the call to action."""
        yield f"# {params.title}"
        if params.subtitle:
            yield f"*{params.subtitle}*"
        yield ""
        for block in markdown_blocks(params.content):
            if block.is_heading:
                yield from ("---", "", block.source)
            else:
                yield from emailable(block.text)
            yield ""
        if params.cta:
            yield from ("---", "", f"*{params.cta}*", "")

    result = "\n".join(rendered()).strip()
    return FormatNewsletterOutput(content=result, word_count=len(result.split()))


async def do_format_custom(params: FormatCustomInput) -> FormatCustomOutput:
    """Reformat content according to a freeform format description via LLM query."""
    from inkwell.agent.client import query

    class FormattedContent(BaseModel):
        content: str = Field(description="The reformatted content")

    content_path = save_content_for_query(params.content, "custom")
    prompt = (
        f"Reformat the following content according to these format instructions.\n\n"
        f"**Format:** {params.format_description}\n"
    )
    if params.title:
        prompt += f"**Title/Subject:** {params.title}\n"
    prompt += f"\nRead the content from: {content_path}"

    result = await query(
        prompt,
        output_type=FormattedContent,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=(
            "You are a format adapter. Rewrite the content to match the requested "
            "format exactly — adopt the conventions, structure, tone, and layout "
            "described. Preserve all substantive content and arguments. Output only "
            "the reformatted text, no meta-commentary."
        ),
    )
    content = result.content if result else params.content

    return FormatCustomOutput(
        content=content,
        word_count=len(content.split()),
    )


LINKEDIN_WRITER_SYSTEM = """\
You rewrite articles as LinkedIn posts for a professional audience. This is a
rewrite, not a summary: keep the author's voice, claims, and specific numbers.

Rules:
- Open with a one-line hook that earns the "see more" expand
- Short paragraphs (1-3 sentences) with generous line breaks for skimmability
- Land 2-4 concrete points from the article; cut prose connective tissue
- Professional but conversational; first person is fine; emojis sparingly
- Close with a reflection or a question that invites comments
- Target 150-400 words; no markdown headers, no "thread 🧵" clichés"""


async def do_format_linkedin(params: FormatLinkedinInput) -> FormatLinkedinOutput:
    """Rewrite content as a LinkedIn post via LLM."""
    from inkwell.agent.client import query

    class LinkedinPost(BaseModel):
        content: str = Field(description="The LinkedIn post body in markdown")

    if params.hashtags:
        tags = " ".join("#" + h.removeprefix("#") for h in params.hashtags)
        hashtag_directive = f"End with these hashtags: {tags}"
    else:
        hashtag_directive = "End with 3-5 fitting hashtags."

    content_path = save_content_for_query(params.content, "linkedin")
    result = await query(
        (
            f"Rewrite this article as a LinkedIn post.\n"
            f"Topic: {params.title}\n"
            f"{hashtag_directive}\n\n"
            f"Read the article from: {content_path}"
        ),
        output_type=LinkedinPost,
        model=stage_model("format"),
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        system_prompt=LINKEDIN_WRITER_SYSTEM,
    )
    content = result.content if result else params.content
    return FormatLinkedinOutput(content=content, word_count=len(content.split()))
