# claude: ignore
# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false
"""Unified writing pipeline with restartable state machine."""

import asyncio
import logging
import tempfile
import unicodedata
from pathlib import Path

from claude_agent_sdk import McpServerConfig

from lup.client import CostAccumulator, HeartbeatCallback, query
from lup.mcp import LupMcpTool, create_mcp_server, extract_sdk_tools
from lup.trace import TraceLogger

from inkwell.agent.config import settings
from inkwell.agent.models import (
    AddAction,
    ArticlePlan,
    AssumptionsList,
    DropAction,
    MergedDraft,
    PatchAction,
    PipelineSnapshot,
    PreserveAction,
    ResearchCompilation,
    RestartStrategy,
    ReviewFinding,
    ReviewOutput,
    RewriteAction,
    SectionDraft,
    SectionPlan,
    WritingOutput,
)
from lup.background import BackgroundAgent

from inkwell.agent.notes import PipelineNotes
from inkwell.agent.watcher import create_comment_watcher
from inkwell.agent.session import WritingSessionState
from inkwell.agent.stages import (
    ASSUMPTIONS_PROMPT,
    COHERENCE_EDITOR_PROMPT,
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
    ORCHESTRATOR_PROMPT,
    PLANNER_SYSTEM,
    REFINER_SYSTEM,
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    SECTION_WRITER_PROMPT,
    STYLE_REVIEWER_PROMPT,
)
from inkwell.agent.tool_policy import research_tool_names, review_tool_names
from inkwell.agent.tools.extract import do_extract_source
from inkwell.agent.tools.formats import (
    FormatBlogInput,
    FormatDialogInput,
    FormatLesswrongInput,
    FormatTwitterInput,
    do_format_blog,
    do_format_dialog,
    do_format_lesswrong,
    do_format_twitter,
)
from inkwell.agent.tools.google_docs import (
    configure_session_state,
    do_create_doc,
    do_create_tab,
    do_insert_comment,
    do_list_tabs,
    do_read_tab,
    do_rename_doc,
    do_write_tab,
)
from inkwell.agent.tools.research.arxiv import ARXIV_TOOLS
from inkwell.agent.tools.research.exa import EXA_TOOLS
from inkwell.agent.tools.research.fetch import FETCH_TOOLS
from inkwell.agent.tools.research.fred import FRED_TOOLS
from inkwell.agent.tools.research.markets import MARKET_TOOLS
from inkwell.agent.tools.research.wikipedia import WIKIPEDIA_TOOLS
from inkwell.agent.tools.voice import VoiceProfile, do_analyze_voice, load_style_corpus

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Raised when a pipeline stage fails to produce valid output."""


GDOC_CHAR_MAP = [
    ("‘", "'"), ("’", "'"),
    ("“", '"'), ("”", '"'),
    ("—", "--"), ("–", "-"),
    (" ", " "),
]


def normalize_gdoc_text(text: str) -> str:
    """Normalize text for comparison, stripping GDoc formatting artifacts."""
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.split())
    for old, new in GDOC_CHAR_MAP:
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------
# Pipeline listener — override for interactive behavior
# ---------------------------------------------------------------------------


class PipelineListener:
    """Receives pipeline events and collects author feedback.

    Default implementation logs to the standard logger. Subclass for
    interactive terminal display, GUI hooks, etc.
    """

    async def on_stage(self, stage: str, description: str) -> None:
        """Called when a pipeline stage begins."""
        logger.info("Pipeline: %s — %s", stage, description)

    async def on_progress(self, message: str) -> None:
        """Mid-stage progress update."""
        logger.info(message)

    async def on_complete(self, output: WritingOutput) -> None:
        """Called when the pipeline finishes."""
        logger.info("Pipeline complete: '%s'", output.title)

    async def on_message(self, source: str, message: str) -> None:
        """Informational message from a pipeline stage."""
        logger.info("[%s] %s", source, message)

    async def collect_feedback(self, state: WritingSessionState) -> list[str]:
        """Gather author feedback from all channels at a stage boundary.

        Returns a list of tagged context strings to pass to the next stage.
        The default implementation checks Google Doc comments.
        """
        comments = state.get_new_author_comments()
        feedback: list[str] = []
        for c in comments:
            if c["reply"]:
                line = f'[GDoc Reply] Author reply to "{c["content"]}": {c["reply"]}'
            else:
                line = f"[GDoc Comment] {c['content']}"
                if c["anchor_text"]:
                    line += f' (on: "{c["anchor_text"]}")'
            feedback.append(line)
        return feedback

    async def collect_revision(self, state: WritingSessionState) -> str | None:
        """After pipeline completes, collect revision instructions.

        Returns revision text or None to end the session.
        Default (batch mode) returns None immediately.
        """
        return None


# ---------------------------------------------------------------------------
# Tab edit detection
# ---------------------------------------------------------------------------


class TabTracker:
    """Tracks written tab content and detects author edits/suggestions.

    After writing to a tab, call ``record(tab_id, label, content)`` to store
    what was written. At feedback checkpoints, call ``detect_edits()`` to
    re-read tabs (with suggestions previewed) and return any that differ.
    """

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.written: dict[str, tuple[str, str]] = {}

    def record(self, tab_id: str, label: str, content: str) -> None:
        self.written[tab_id] = (label, content)

    async def detect_edits(self) -> list[str]:
        """Re-read all tracked tabs and return feedback for any that changed."""
        edits: list[str] = []
        for tab_id, (label, original) in self.written.items():
            try:
                current = await do_read_tab(
                    self.doc_id, tab_id, accept_suggestions=True
                )
            except (RuntimeError, OSError):
                continue
            if not self.has_meaningful_diff(original, current):
                continue
            edits.append(
                f"[GDoc Edit: {label}] Author modified this tab. "
                f"Current content:\n{current.strip()}"
            )
        return edits

    @staticmethod
    def has_meaningful_diff(original: str, current: str) -> bool:
        """Compare ignoring GDoc formatting artifacts (whitespace, smart quotes)."""
        if not current.strip():
            return False
        return normalize_gdoc_text(original) != normalize_gdoc_text(current)


# ---------------------------------------------------------------------------
# MCP server builder (research tools for pipeline stages)
# ---------------------------------------------------------------------------


def build_research_tools() -> list[LupMcpTool]:
    """Flat list of all research MCP tools."""
    return [
        *EXA_TOOLS,
        *ARXIV_TOOLS,
        *FETCH_TOOLS,
        *FRED_TOOLS,
        *MARKET_TOOLS,
        *WIKIPEDIA_TOOLS,
    ]


def build_research_servers() -> dict[str, McpServerConfig]:
    """MCP servers for research-capable stages."""
    research_server = create_mcp_server(
        name="research",
        version="1.0.0",
        tools=extract_sdk_tools(build_research_tools()),
    )
    return {"research": research_server}


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def format_feedback(feedback: list[str]) -> str:
    if not feedback:
        return ""
    lines = "\n".join(f"- {f}" for f in feedback)
    return f"\n\n## Author Feedback (priority)\n\n{lines}\n"


def annotate_draft_with_findings(
    draft: str, findings: list[ReviewFinding]
) -> str:
    """Inject review findings inline at matching text_excerpt locations.

    Findings with text_excerpt are anchored next to the passage they refer to.
    Remaining findings are appended as an "Additional Findings" section.
    """
    annotated = draft

    anchorable = [
        f for f in findings
        if f.text_excerpt and f.severity in ("critical", "suggestion")
    ]
    anchorable.sort(key=lambda f: len(f.text_excerpt), reverse=True)

    anchored: set[int] = set()
    for f in anchorable:
        annotation = (
            f"\n[FINDING:{f.severity}:{f.reviewer}] {f.issue}"
            f"\n  → {f.suggestion}\n"
        )
        if f.text_excerpt in annotated:
            annotated = annotated.replace(  # claude: ignore
                f.text_excerpt,
                f"{f.text_excerpt}{annotation}",
                1,
            )
            anchored.add(id(f))

    remaining = [
        f for f in findings
        if f.severity in ("critical", "suggestion") and id(f) not in anchored
    ]
    if remaining:
        annotated += "\n\n## Additional Review Findings\n"
        for f in remaining:
            annotated += (
                f"- [{f.severity}:{f.reviewer}] {f.location}: {f.issue}\n"
                f"  → {f.suggestion}\n"
            )

    return annotated


def format_voice_profile(profile: VoiceProfile) -> str:
    parts = [f"\n\n## Voice Profile\n\n{profile.voice}"]
    if profile.phrases:
        items = "\n".join(f"- {p}" for p in profile.phrases)
        parts.append(f"\n\n### Characteristic Phrases\n\n{items}")
    if profile.avoid:
        items = "\n".join(f"- {a}" for a in profile.avoid)
        parts.append(f"\n\n### Avoid\n\n{items}")
    return "".join(parts)


async def plan_article(
    conversation: str,
    *,
    target_format: str = "lesswrong",
    voice_profile: VoiceProfile | None = None,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> ArticlePlan:
    """Stage 1: Extract a structured article plan from source material."""
    voice_section = ""
    corpus_samples, corpus_sources = load_style_corpus(max_samples=3)
    if corpus_samples:
        voice_section = (
            "\n\n## Style Corpus (author's past writing)\n\n"
            + "\n\n---\n\n".join(s[:1500] for s in corpus_samples)
        )
    if voice_profile:
        voice_section += format_voice_profile(voice_profile)

    format_hint = (
        f"Suggested format: {target_format} (override if the content is better "
        f"suited to another format: lesswrong, twitter, blog, dialog, memo)"
        if target_format
        else "Choose the best output format: lesswrong, twitter, blog, dialog, or memo"
    )
    task = (
        f"Extract a structured article plan from this conversation.\n"
        f"{format_hint}\n\n"
        f"<conversation>\n{conversation}\n</conversation>"
        f"{voice_section}"
    )

    plan = await query(
        task,
        output_type=ArticlePlan,
        model="claude-opus-4-6",
        system_prompt=PLANNER_SYSTEM,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        prefix="[plan] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    if plan is None:
        raise PipelineError("Planner produced no structured output")
    return plan


async def refine_plan(
    plan: ArticlePlan,
    research: ResearchCompilation,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ArticlePlan:
    """Refine the initial plan using research findings."""
    sections_text = "\n\n".join(
        f"### {s.title}\n{s.summary}\nKey points: {', '.join(s.key_points)}"
        for s in plan.sections
    )
    findings_text = "\n\n".join(
        f"### {f.question}\n{f.answer}\nConfidence: {f.confidence}"
        for f in research.findings
    )
    quotes_text = "\n".join(
        f'- "{q.text}" — {q.speaker}: {q.context}' for q in plan.source_quotes
    )

    voice_section = ""
    if voice_profile:
        voice_section = format_voice_profile(voice_profile)

    task = (
        f"Refine this article plan using the research findings below.\n\n"
        f"# Initial Plan: {plan.title}\n\n"
        f"**Thesis:** {plan.thesis}\n\n"
        f"**Target format:** {plan.target_format}\n\n"
        f"**Author direction:** {plan.author_direction}\n\n"
        f"{format_feedback(feedback or [])}"
        f"## Sections\n\n{sections_text}\n\n"
        f"## Source Quotes\n\n{quotes_text or '(none)'}\n\n"
        f"## Research Findings\n\n{findings_text}\n\n"
        f"Return the refined plan with detailed key_points per section."
        f"{voice_section}"
    )

    refined = await query(
        task,
        output_type=ArticlePlan,
        model="claude-opus-4-6",
        system_prompt=REFINER_SYSTEM,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        prefix="[refine] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    if refined is None:
        return plan
    return refined


async def research_plan(
    plan: ArticlePlan,
    *,
    feedback: list[str] | None = None,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> ResearchCompilation:
    """Stage 2: Deep research on all questions from the article plan."""
    if servers is None:
        servers = build_research_servers()

    questions_text = "\n".join(
        f"- [{q.priority}] {q.question} (section: {q.section})"
        for q in plan.research_questions
    )
    quotes_text = "\n".join(
        f'- "{q.text}" — {q.speaker}: {q.context}' for q in plan.source_quotes
    )

    task = (
        f'Research the following questions for an article titled "{plan.title}".\n\n'
        f"Thesis: {plan.thesis}\n\n"
        f"{format_feedback(feedback or [])}"
        f"## Research Questions\n{questions_text}\n\n"
        f"## Source Quotes (for awareness)\n{quotes_text}\n\n"
        f"Be thorough. Cross-reference claims. Prefer primary sources."
    )

    research = await query(
        task,
        output_type=ResearchCompilation,
        model="claude-opus-4-6",
        system_prompt=RESEARCHER_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=servers,
        allowed_tools=research_tool_names(),
        trace_logger=trace_logger,
        prefix="[research] ",

        cost_accumulator=cost_accumulator,
    )
    if research is None:
        raise PipelineError("Researcher produced no structured output")
    return research


async def write_section(
    section_plan: SectionPlan,
    research: ResearchCompilation,
    plan: ArticlePlan,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> SectionDraft:
    """Write a single section. Called in parallel for all sections."""
    if servers is None:
        servers = build_research_servers()

    relevant = [
        f
        for f in research.findings
        if f.question
        in [
            q.question
            for q in plan.research_questions
            if q.section == section_plan.title
        ]
    ]
    findings_text = "\n\n".join(
        f"### {f.question}\n{f.answer}\n"
        f"Sources: {', '.join(s.url for s in f.sources)}\n"
        f"Confidence: {f.confidence}"
        for f in relevant
    )
    if not findings_text:
        findings_text = "\n\n".join(
            f"### {f.question}\n{f.answer}" for f in research.findings
        )

    quotes = [
        q
        for q in plan.source_quotes
        if q.text in (section_plan.quotes_to_include or [])
    ]
    quotes_text = "\n".join(f'- "{q.text}" — {q.speaker}' for q in quotes)

    voice_section = f"## Voice\n{plan.voice_notes}"
    if voice_profile:
        voice_section += format_voice_profile(voice_profile)

    task = (
        f'Write the section "{section_plan.title}" for the article "{plan.title}".\n\n'
        f"## Section Plan\n"
        f"Summary: {section_plan.summary}\n"
        f"Key points: {', '.join(section_plan.key_points)}\n\n"
        f"{format_feedback(feedback or [])}"
        f"## Research Findings\n{findings_text}\n\n"
        f"## Quotes to Weave In\n{quotes_text or '(none)'}\n\n"
        f"{voice_section}\n\n"
        f"## Author Direction\n{plan.author_direction}\n\n"
        f"Return the section content as markdown in the 'content' field."
    )

    draft = await query(
        task,
        output_type=SectionDraft,
        model="claude-opus-4-6",
        system_prompt=SECTION_WRITER_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=servers,
        allowed_tools=research_tool_names(),
        trace_logger=trace_logger,
        prefix=f"[write:{section_plan.title}] ",

        cost_accumulator=cost_accumulator,
    )
    if draft is None:
        raise PipelineError(
            f"Section writer for '{section_plan.title}' produced no output"
        )
    return draft


async def merge_sections(
    drafts: list[SectionDraft],
    plan: ArticlePlan,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    heartbeat: HeartbeatCallback | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> MergedDraft:
    """Stage 4: Merge independently-written sections into a coherent draft."""
    valid_drafts = [d for d in drafts if not d.content.startswith("[Section failed")]
    if not valid_drafts:
        raise PipelineError("All sections failed — nothing to merge")

    sections_text = "\n\n---\n\n".join(
        f"## {d.title}\n\n{d.content}" for d in valid_drafts
    )

    voice_section = ""
    if voice_profile:
        voice_section = format_voice_profile(voice_profile)

    task = (
        f'Rewrite these sections into a unified article: "{plan.title}"\n\n'
        f"Thesis: {plan.thesis}\n\n"
        f"{format_feedback(feedback or [])}"
        f"## Source Sections\n\n{sections_text}\n\n"
        f"These are raw material, not a starting draft. Write a new, "
        f"unified piece that makes this thesis land. Cut freely, "
        f"reorganize where the argument flows better, and ensure "
        f"every paragraph earns its place."
        f"{voice_section}"
    )

    merged = await query(
        task,
        output_type=MergedDraft,
        model="claude-opus-4-6",
        system_prompt=COHERENCE_EDITOR_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        trace_logger=trace_logger,
        prefix="[merge] ",
        heartbeat=heartbeat,
        cost_accumulator=cost_accumulator,
    )
    if merged is None:
        raise PipelineError("Coherence editor produced no structured output")
    return merged


async def review_narrative(
    draft: str,
    plan: ArticlePlan,
    *,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for narrative coherence (tool-free)."""
    task = (
        f"Review this article draft for narrative coherence.\n\n"
        f"Thesis: {plan.thesis}\n\n"
        f"<draft>\n{draft}\n</draft>"
    )
    result = await query(
        task,
        output_type=ReviewOutput,
        model="claude-opus-4-6",
        system_prompt=NARRATIVE_REVIEWER_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",

        trace_logger=trace_logger,
        prefix="[review:narrative] ",

        cost_accumulator=cost_accumulator,
    )
    if result is None:
        return ReviewOutput(findings=[])
    for f in result.findings:
        f.reviewer = "narrative"
    return result


async def review_facts(
    draft: str,
    plan: ArticlePlan,
    *,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for factual accuracy (needs research tools to verify)."""
    if servers is None:
        servers = build_research_servers()

    task = (
        f"Fact-check every verifiable claim in this article draft.\n\n"
        f"<draft>\n{draft}\n</draft>"
    )
    result = await query(
        task,
        output_type=ReviewOutput,
        model="claude-opus-4-6",
        system_prompt=FACT_CHECKER_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=servers,
        allowed_tools=review_tool_names(),
        trace_logger=trace_logger,
        prefix="[review:facts] ",

        cost_accumulator=cost_accumulator,
    )
    if result is None:
        return ReviewOutput(findings=[])
    for f in result.findings:
        f.reviewer = "factcheck"
    return result


async def review_style(
    draft: str,
    plan: ArticlePlan,
    *,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for writing quality and voice consistency (tool-free)."""
    task = (
        f"Review this article draft for writing quality and style.\n\n"
        f"Voice target: {plan.voice_notes}\n\n"
        f"<draft>\n{draft}\n</draft>"
    )
    result = await query(
        task,
        output_type=ReviewOutput,
        model="claude-opus-4-6",
        system_prompt=STYLE_REVIEWER_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",

        trace_logger=trace_logger,
        prefix="[review:style] ",

        cost_accumulator=cost_accumulator,
    )
    if result is None:
        return ReviewOutput(findings=[])
    for f in result.findings:
        f.reviewer = "style"
    return result


async def review_all(
    draft: str,
    plan: ArticlePlan,
    *,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> list[ReviewFinding]:
    """Stage 5: Run all three reviewers in parallel."""
    narrative_task = review_narrative(
        draft,
        plan,
        trace_logger=trace_logger,

        cost_accumulator=cost_accumulator,
    )
    facts_task = review_facts(
        draft,
        plan,
        servers=servers,
        trace_logger=trace_logger,

        cost_accumulator=cost_accumulator,
    )
    style_task = review_style(
        draft,
        plan,
        trace_logger=trace_logger,

        cost_accumulator=cost_accumulator,
    )

    results = await asyncio.gather(
        narrative_task,
        facts_task,
        style_task,
        return_exceptions=True,
    )

    all_findings: list[ReviewFinding] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Reviewer failed: %s", result)
        else:
            all_findings.extend(result.findings)

    return all_findings


async def rewrite_final(
    draft: str,
    findings: list[ReviewFinding],
    plan: ArticlePlan,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    target_format: str = "lesswrong",
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Stage 6: Incorporate all feedback and produce the final article."""
    annotated_draft = annotate_draft_with_findings(draft, findings)

    n_critical = sum(1 for f in findings if f.severity == "critical")
    n_suggestion = sum(1 for f in findings if f.severity == "suggestion")

    voice_section = ""
    if voice_profile:
        voice_section = format_voice_profile(voice_profile)

    task = (
        f'Produce the final version of "{plan.title}".\n\n'
        f"Target format: {target_format}\n\n"
        f"Review summary: {n_critical} critical (mandatory), "
        f"{n_suggestion} suggestions\n\n"
        f"{format_feedback(feedback or [])}"
        f"## Annotated Draft\n\n{annotated_draft}\n\n"
        f"## Author Direction\n{plan.author_direction}\n\n"
        f"Apply ALL critical findings — they are marked inline. "
        f"Return the full article text in the 'content' field "
        f"and a brief 1-2 sentence editorial summary in the 'summary' field."
        f"{voice_section}"
    )

    output = await query(
        task,
        output_type=WritingOutput,
        model="claude-opus-4-6",
        system_prompt=REWRITER_SYSTEM,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",

        trace_logger=trace_logger,
        prefix="[rewrite] ",

        cost_accumulator=cost_accumulator,
    )
    if output is None:
        raise PipelineError("Rewriter produced no structured output")
    output.review_findings = findings
    return output


async def apply_format(
    content: str,
    title: str,
    target_format: str,
) -> str:
    """Apply format-specific transformations to the final content."""
    match target_format:
        case "lesswrong":
            result = do_format_lesswrong(
                FormatLesswrongInput(
                    content=content,
                    epistemic_status="Moderately confident",
                )
            )
            return result.content
        case "twitter":
            first_line = content.split("\n", 1)[0].lstrip("#").strip()
            result = do_format_twitter(
                FormatTwitterInput(
                    content=content,
                    hook=first_line[:260],
                )
            )
            return "\n\n---\n\n".join(result.tweets)
        case "blog":
            result = do_format_blog(
                FormatBlogInput(
                    content=content,
                    title=title,
                )
            )
            return result.content
        case "dialog":
            result = await do_format_dialog(
                FormatDialogInput(content=content)
            )
            return result.compiled
        case "memo":
            from inkwell.agent.tools.formats import FormatMemoInput, do_format_memo

            result = do_format_memo(
                FormatMemoInput(content=content, title=title)
            )
            return result.content
        case _ if target_format.startswith("custom:"):
            from inkwell.agent.tools.formats import FormatCustomInput, do_format_custom

            description = target_format.removeprefix("custom:").strip()
            result = await do_format_custom(
                FormatCustomInput(
                    content=content,
                    format_description=description,
                    title=title,
                )
            )
            return result.content
        case _:
            return content


# ---------------------------------------------------------------------------
# New stages (assumptions, orchestrator)
# ---------------------------------------------------------------------------


async def surface_assumptions(
    plan: ArticlePlan,
    doc_id: str,
    *,
    research: ResearchCompilation | None = None,
    session_state: WritingSessionState,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> AssumptionsList:
    """Surface uncertainties and questions from the plan as GDoc comments."""
    sections_text = "\n\n".join(
        f"### {s.title}\n{s.summary}\nKey points: {', '.join(s.key_points)}"
        for s in plan.sections
    )
    task = (
        f'Review this article plan and surface all uncertainties.\n\n'
        f"# {plan.title}\n\n"
        f"**Thesis:** {plan.thesis}\n\n"
        f"**Author direction:** {plan.author_direction}\n\n"
        f"## Sections\n\n{sections_text}"
    )
    if research:
        findings_summary = "\n".join(
            f"- **{f.question}** — confidence: {f.confidence:.0%}"
            + (f" ({f.answer[:150]}...)" if f.confidence < 0.5 else "")
            for f in research.findings
        )
        task += (
            f"\n\n## Research Findings (for context)\n\n{findings_summary}"
            f"\n\nResearch has been completed. Focus on questions that "
            f"research could NOT answer, contradictions between sources, "
            f"and directional choices the author needs to make."
        )

    result = await query(
        task,
        output_type=AssumptionsList,
        model="claude-opus-4-6",
        system_prompt=ASSUMPTIONS_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        prefix="[assumptions] ",
        trace_logger=trace_logger,

        cost_accumulator=cost_accumulator,
    )
    if result is None:
        return AssumptionsList(items=[])

    tag_prefix = {
        "direction_check": "[DIRECTION]",
        "assumption": "[ASSUMPTION]",
        "question": "[QUESTION]",
        "confusion": "[UNCLEAR]",
    }
    for item in result.items:
        prefix = tag_prefix.get(item.tag, "[NOTE]")
        comment_text = (
            f"{prefix} {item.content}\n\n"
            f"My best guess: {item.best_guess}"
        )
        await do_insert_comment(
            doc_id, comment_text,
            anchor_text=item.anchor_section,
            session_state=session_state,
        )

    return result


async def plan_restart(
    snapshot: PipelineSnapshot,
    notes: PipelineNotes,
    *,
    trace_logger: TraceLogger | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> RestartStrategy:
    """Run the orchestrator to produce a fine-grained restart strategy."""
    plan = snapshot.plan
    if plan is None:
        raise PipelineError("Cannot restart without a plan")

    feedback_text = await notes.get_all_feedback()

    drafts_text = ""
    if snapshot.section_drafts:
        drafts_text = "\n\n---\n\n".join(
            f"## {title}\n\n{draft.content}"
            for title, draft in snapshot.section_drafts.items()
        )

    sections_text = "\n".join(
        f"- {s.title}: {s.summary}" for s in plan.sections
    )

    task = (
        f"Determine how to handle author feedback for this article.\n\n"
        f"# Current Plan: {plan.title}\n\n"
        f"**Thesis:** {plan.thesis}\n\n"
        f"## Sections\n{sections_text}\n\n"
    )
    if drafts_text:
        task += f"## Current Drafts\n\n{drafts_text}\n\n"
    task += f"{feedback_text}"

    strategy = await query(
        task,
        output_type=RestartStrategy,
        model="claude-opus-4-6",
        system_prompt=ORCHESTRATOR_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",

        prefix="[orchestrator] ",
        trace_logger=trace_logger,

        cost_accumulator=cost_accumulator,
    )
    if strategy is None:
        raise PipelineError("Orchestrator produced no strategy")
    return strategy


# ---------------------------------------------------------------------------
# Pipeline runner (state machine)
# ---------------------------------------------------------------------------


class PipelineRunner:
    """State-machine wrapper around the pipeline stages.

    Accumulates artifacts in PipelineSnapshot and checks for plan-breaking
    author feedback between stages via asyncio.Event. When triggered, runs
    the orchestrator to produce a RestartStrategy and executes it.
    """

    def __init__(
        self,
        source: str,
        *,
        target_format: str = "lesswrong",
        existing_doc_id: str | None = None,
        session_state: WritingSessionState | None = None,
        notes: PipelineNotes | None = None,
        trace_logger: TraceLogger | None = None,
        refs: list[str] | None = None,
        listener: PipelineListener | None = None,
    
        cost_accumulator: CostAccumulator | None = None,
    ) -> None:
        self.source = source
        self.target_format = target_format
        self.existing_doc_id = existing_doc_id
        self.state = session_state or WritingSessionState()
        self.notes = notes
        self.trace_logger = trace_logger
        self.refs = refs or []
        self.hooks = listener or PipelineListener()

        if cost_accumulator is None:
            cost_accumulator = CostAccumulator()
        self.cost_accumulator = cost_accumulator

        self.snapshot = PipelineSnapshot()
        self.plan_breaking = asyncio.Event()
        self.restart_count = 0
        self.max_restarts = 2

        self.research_servers = build_research_servers()

        self.doc_id = ""
        self.doc_url = ""
        self.known_tabs: dict[str, str] = {}
        self.tab_ids: dict[str, str] = {}
        self.tabs: TabTracker | None = None

        self.overview_tab_id = ""
        self.draft_tab_id = ""
        self.final_tab_id = ""

        self.watcher: BackgroundAgent | None = None

    async def save_snapshot(self) -> None:
        """Persist the current snapshot for resume support."""
        if self.notes is None:
            return
        data = self.snapshot.model_dump_json()
        base = self.notes.base_dir
        base.mkdir(parents=True, exist_ok=True)
        (base / "snapshot.json").write_text(data, encoding="utf-8")
        (base / f"snapshot_{self.snapshot.stage}.json").write_text(data, encoding="utf-8")

    async def run(self) -> WritingOutput:
        """Execute the full pipeline with restart support."""
        configure_session_state(self.state)
        await self.setup_doc()

        await self.stage_extract()
        await self.stage_voice()
        await self.stage_plan()

        await self.stage_research()
        await self.check_and_maybe_restart()

        await self.stage_assumptions()
        await self.stage_refine()

        self.start_watcher()

        try:

            await self.stage_write()
            await self.check_and_maybe_restart()

            await self.stage_merge()
            await self.stage_review()
            await self.check_and_maybe_restart()

            await self.stage_rewrite()
            await self.stage_format()

            output = self.snapshot.output
            if output is None:
                raise PipelineError("Pipeline completed without producing output")

            await self.update_overview()
            await self.hooks.on_complete(output)

            await self.standby_loop()
        finally:
            await self.stop_watcher()

        output = self.snapshot.output
        if output is None:
            raise PipelineError("Pipeline completed without producing output")
        return output

    async def run_from(self, snapshot: PipelineSnapshot) -> WritingOutput:
        """Resume pipeline execution from a saved snapshot."""
        self.snapshot = snapshot
        configure_session_state(self.state)

        if snapshot.output and snapshot.output.google_doc_id:
            self.existing_doc_id = snapshot.output.google_doc_id

        await self.setup_doc()

        stages = [
            "extract", "voice", "plan",
            "research", "assumptions", "refine",
            "write", "merge", "review", "rewrite", "format",
        ]
        last_idx = stages.index(snapshot.stage) if snapshot.stage in stages else -1
        remaining = stages[last_idx + 1:]

        if snapshot.plan:
            self.start_watcher()

        try:
            for stage_name in remaining:
                method = getattr(self, f"stage_{stage_name}")
                await method()
                if stage_name in ("research", "write", "review"):
                    await self.check_and_maybe_restart()

            output = self.snapshot.output
            if output is None:
                raise PipelineError("Pipeline completed without producing output")

            await self.update_overview()
            await self.hooks.on_complete(output)
            await self.standby_loop()
        finally:
            await self.stop_watcher()

        output = self.snapshot.output
        if output is None:
            raise PipelineError("Pipeline completed without producing output")
        return output

    def start_watcher(self) -> None:
        """Start the Comment Watcher if notes are available."""
        if self.notes is None:
            return
        self.watcher = create_comment_watcher(
            session_state=self.state,
            notes=self.notes,
            plan_breaking_signal=self.plan_breaking,
        )
        self.watcher.start()
        logger.info("Comment watcher started")

    async def stop_watcher(self) -> None:
        """Stop the Comment Watcher if running."""
        if self.watcher is not None:
            await self.watcher.stop()
            self.watcher = None
            logger.info("Comment watcher stopped")

    async def setup_doc(self) -> None:
        """Create or reuse the Google Doc and standard tabs."""
        if self.existing_doc_id:
            self.doc_id = self.existing_doc_id
            self.doc_url = f"https://docs.google.com/document/d/{self.doc_id}/edit"
            self.state.set_doc(self.doc_id, self.doc_url)
            await self.hooks.on_progress(f"Reusing Google Doc: {self.doc_url}")
            existing = await do_list_tabs(self.doc_id)
            self.known_tabs = {t.title: t.tab_id for t in existing}
        else:
            self.doc_id, self.doc_url = await do_create_doc(
                f"Inkwell — {self.source[:60]}",
                share_with=settings.author_email,
                session_state=self.state,
            )
            await self.hooks.on_progress(f"Google Doc: {self.doc_url}")

        for tab_name in ("Overview", "Source", "Voice", "Plan", "Research"):
            if tab_name not in self.known_tabs:
                tid = await do_create_tab(
                    self.doc_id, tab_name, session_state=self.state
                )
                self.known_tabs[tab_name] = tid

        self.overview_tab_id = self.known_tabs["Overview"]
        self.tabs = TabTracker(self.doc_id)

        await do_write_tab(
            self.doc_id,
            self.overview_tab_id,
            (
                f"# Writing Pipeline\n\n"
                f"**Source:** {self.source[:120]}\n\n"
                f"**Stage:** starting\n\n"
                f"*Comment on any tab to give feedback. "
                f"Edit directly to override agent decisions.*"
            ),
            session_state=self.state,
        )

    async def gather_feedback(self) -> list[str]:
        """Collect feedback from GDoc comments and tab edits."""
        assert self.tabs is not None
        comment_feedback = await self.hooks.collect_feedback(self.state)
        edit_feedback = await self.tabs.detect_edits()
        all_feedback = comment_feedback + edit_feedback

        if self.notes and all_feedback:
            for fb in all_feedback:
                await self.notes.add_terminal_input(fb)

        return all_feedback

    async def stage_extract(self) -> None:
        """Extract source material."""
        await self.hooks.on_stage("extract", "Extracting source material")
        await self.update_overview(active_stage="extract")
        conversation = await do_extract_source(self.source)

        for ref in self.refs:
            try:
                ref_text = await do_extract_source(ref)
                conversation += f"\n\n--- Reference: {ref} ---\n\n{ref_text}"
            except RuntimeError:
                logger.warning("Failed to extract reference: %s", ref)

        self.snapshot.conversation = conversation
        self.snapshot.stage = "extract"
        await self.save_snapshot()

        source_tab_id = self.known_tabs["Source"]
        await do_write_tab(
            self.doc_id, source_tab_id, conversation, session_state=self.state
        )
        assert self.tabs is not None
        # Source tab is reference material — don't track for edit detection.
        # TabTracker re-reads tracked tabs and any content difference (even
        # from GDoc formatting) gets injected as "author feedback" into every
        # downstream stage, causing section writers to anchor on the source.
        await self.update_overview()

    async def stage_voice(self) -> None:
        """Analyze the author's writing voice."""
        await self.hooks.on_stage("voice", "Analyzing author's writing voice")
        await self.update_overview(active_stage="voice")
        corpus_samples, _ = load_style_corpus(max_samples=3)
        voice_profile = await do_analyze_voice(
            self.snapshot.conversation,
            corpus_samples,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        if voice_profile is None:
            raise PipelineError("Voice analysis produced no structured output")
        self.snapshot.voice_profile = voice_profile
        self.snapshot.stage = "voice"
        await self.save_snapshot()

        if voice_profile:
            summary = voice_profile.voice[:80].rsplit(" ", 1)[0]
            await self.hooks.on_progress(f"Voice: {summary}...")
            voice_text = f"# Voice Profile\n\n{voice_profile.voice}"
            if voice_profile.phrases:
                voice_text += "\n\n## Characteristic Phrases\n\n" + "\n".join(
                    f"- {p}" for p in voice_profile.phrases
                )
            if voice_profile.avoid:
                voice_text += "\n\n## Avoid\n\n" + "\n".join(
                    f"- {a}" for a in voice_profile.avoid
                )
            voice_tab_id = self.known_tabs["Voice"]
            await do_write_tab(
                self.doc_id, voice_tab_id, voice_text, session_state=self.state
            )
            assert self.tabs is not None
            # Voice tab is reference — don't track for edits
        await self.update_overview()

    async def stage_plan(self) -> None:
        """Plan the article structure."""
        await self.hooks.on_stage("plan", "Planning article structure")
        await self.update_overview(active_stage="plan")
        plan = await plan_article(
            self.snapshot.conversation,
            target_format=self.target_format,
            voice_profile=self.snapshot.voice_profile,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.plan = plan
        self.snapshot.stage = "plan"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Plan: '{plan.title}' — {len(plan.sections)} sections, "
            f"{len(plan.research_questions)} research questions"
        )
        await self.write_plan_tab(plan)
        await self.create_section_tabs(plan)
        await self.update_overview()

    async def stage_assumptions(self) -> None:
        """Surface uncertainties as GDoc comments (non-blocking)."""
        plan = self.snapshot.plan
        if plan is None:
            return
        await self.hooks.on_stage("assumptions", "Surfacing questions for the author")
        await self.update_overview(active_stage="assumptions")
        assumptions = await surface_assumptions(
            plan,
            self.doc_id,
            research=self.snapshot.research,
            session_state=self.state,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.stage = "assumptions"
        await self.save_snapshot()
        if assumptions.items:
            await self.hooks.on_progress(
                f"Posted {len(assumptions.items)} questions/assumptions as GDoc comments"
            )
        await self.update_overview()

    async def stage_research(self) -> None:
        """Research all questions from the plan."""
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot research without a plan")

        await self.hooks.on_stage(
            "research",
            f"Researching {len(plan.research_questions)} questions",
        )
        self.state.set_stage("researching")
        await self.update_overview(active_stage="research")
        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("research", feedback)

        research = await research_plan(
            plan,
            feedback=feedback_with_notes,
            servers=self.research_servers,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.research = research
        self.snapshot.stage = "research"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Research complete: {len(research.findings)} findings"
        )
        await self.write_research_tab(research)
        await self.update_overview()

    async def stage_refine(self) -> None:
        """Refine the plan using research findings and any early author feedback."""
        plan = self.snapshot.plan
        research = self.snapshot.research
        if plan is None or research is None:
            return

        await self.hooks.on_stage(
            "refine",
            f"Refining plan with {len(research.findings)} research findings",
        )
        await self.update_overview(active_stage="refine")
        feedback = await self.gather_feedback()
        initial_count = len(plan.sections)
        refined = await refine_plan(
            plan,
            research,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback or None,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.plan = refined
        self.snapshot.stage = "refine"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Refined plan: {len(refined.sections)} sections "
            f"(was {initial_count})"
        )
        await self.write_plan_tab(refined)
        await self.create_section_tabs(refined)
        await self.update_overview()

    async def stage_write(self) -> None:
        """Write all sections in parallel, updating overview as each completes."""
        plan = self.snapshot.plan
        research = self.snapshot.research
        if plan is None or research is None:
            raise PipelineError("Cannot write without plan and research")

        await self.hooks.on_stage(
            "write",
            f"Writing {len(plan.sections)} sections in parallel",
        )
        self.state.set_stage("writing")
        await self.update_overview(active_stage="write")

        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("write", feedback)

        async def write_and_publish(section: SectionPlan) -> SectionDraft:
            draft = await write_section(
                section,
                research,
                plan,
                voice_profile=self.snapshot.voice_profile,
                feedback=feedback_with_notes,
                servers=self.research_servers,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
            draft.title = section.title
            self.snapshot.section_drafts[section.title] = draft

            assert self.tabs is not None
            tid = self.tab_ids.get(section.title, "")
            if tid and draft.content and not draft.content.startswith("[Section failed"):
                await do_write_tab(
                    self.doc_id, tid, draft.content, session_state=self.state
                )
                self.tabs.record(tid, f"Section: {section.title}", draft.content)
                self.state.update_section_status(section.title, "drafted")

            for question in draft.questions_for_author:
                await do_insert_comment(
                    self.doc_id,
                    f"[QUESTION] {question}",
                    session_state=self.state,
                )
                self.state.add_question(question)

            await self.hooks.on_message(
                "write",
                f"Section '{draft.title}' complete ({draft.word_count} words)",
            )
            await self.update_overview(active_stage="write")
            return draft

        results = await asyncio.gather(
            *(write_and_publish(s) for s in plan.sections),
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                title = plan.sections[i].title
                logger.error("Section '%s' failed: %s", title, result)
                self.snapshot.section_drafts[title] = SectionDraft(
                    title=title,
                    content=f"[Section failed: {result}]",
                    word_count=0,
                )

        self.snapshot.stage = "write"
        await self.save_snapshot()
        await self.update_overview()

    async def stage_merge(self) -> None:
        """Merge sections into a coherent draft."""
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot merge without a plan")

        await self.hooks.on_stage("merge", "Merging sections into coherent draft")
        self.state.set_stage("merging")
        await self.update_overview(active_stage="merge")
        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("merge", feedback)

        async def merge_heartbeat(elapsed: float) -> None:
            mins, secs = divmod(int(elapsed), 60)
            await self.hooks.on_progress(f"Merging... ({mins}m{secs:02d}s elapsed)")

        drafts = list(self.snapshot.section_drafts.values())
        merged = await merge_sections(
            drafts,
            plan,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback_with_notes,
            trace_logger=self.trace_logger,
            heartbeat=merge_heartbeat,
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.merged = merged
        self.snapshot.stage = "merge"
        await self.save_snapshot()

        if "Draft" not in self.known_tabs:
            self.known_tabs["Draft"] = await do_create_tab(
                self.doc_id, "Draft", session_state=self.state
            )
        self.draft_tab_id = self.known_tabs["Draft"]
        await do_write_tab(
            self.doc_id, self.draft_tab_id, merged.content, session_state=self.state
        )
        assert self.tabs is not None
        self.tabs.record(self.draft_tab_id, "Draft", merged.content)
        await self.update_overview()

    async def stage_review(self) -> None:
        """Run parallel reviewers on the merged draft."""
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot review without plan and merged draft")

        await self.hooks.on_stage("review", "Reviewing draft (3 reviewers in parallel)")
        self.state.set_stage("reviewing")
        await self.update_overview(active_stage="review")

        findings = await review_all(
            merged.content,
            plan,
            servers=self.research_servers,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.findings = findings
        self.snapshot.stage = "review"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Review complete: {len(findings)} findings "
            f"({sum(1 for f in findings if f.severity == 'critical')} critical)"
        )
        await self.write_review_tab(findings)
        await self.update_overview()

    async def stage_rewrite(self) -> None:
        """Produce the final version incorporating review feedback."""
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot rewrite without plan and merged draft")

        await self.hooks.on_stage("rewrite", "Producing final version")
        self.state.set_stage("rewriting")
        await self.update_overview(active_stage="rewrite")
        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("rewrite", feedback)

        output = await rewrite_final(
            merged.content,
            self.snapshot.findings,
            plan,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback_with_notes,
            target_format=self.target_format,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        output.voice_profile = self.snapshot.voice_profile
        output.open_questions = list(self.state.pending_questions)
        self.snapshot.output = output
        self.snapshot.stage = "rewrite"
        await self.save_snapshot()
        await self.update_overview()

    async def stage_format(self) -> None:
        """Apply format-specific transformations and write to Final tab."""
        output = self.snapshot.output
        plan = self.snapshot.plan
        if output is None or plan is None:
            raise PipelineError("Cannot format without output and plan")

        chosen_format = plan.target_format or self.target_format
        await self.hooks.on_stage("format", f"Applying {chosen_format} formatting")
        await self.update_overview(active_stage="format")
        article_text = output.content or output.summary
        final_content = await apply_format(
            article_text, plan.title, chosen_format
        )

        if "Final" not in self.known_tabs:
            self.known_tabs["Final"] = await do_create_tab(
                self.doc_id, "Final", session_state=self.state
            )
        self.final_tab_id = self.known_tabs["Final"]
        await do_write_tab(
            self.doc_id, self.final_tab_id, final_content, session_state=self.state
        )

        output.google_doc_id = self.doc_id
        output.google_doc_url = self.doc_url
        output.word_count = len(final_content.split())  # claude: ignore
        self.snapshot.stage = "format"
        self.state.set_stage("complete")
        await self.save_snapshot()
        await self.update_overview()

    # -- Restart logic ---------------------------------------------------

    async def check_and_maybe_restart(self) -> None:
        """Check for plan-breaking signals. If found, run orchestrator."""
        has_signal = self.plan_breaking.is_set()
        has_notes = self.notes is not None and await self.notes.has_plan_breaking()

        if not has_signal and not has_notes:
            return
        self.plan_breaking.clear()

        if self.restart_count >= self.max_restarts:
            logger.warning(
                "Max restarts (%d) reached — downgrading to stage-local",
                self.max_restarts,
            )
            return

        logger.info("Plan-breaking feedback detected — running orchestrator")
        await self.hooks.on_stage(
            "restart",
            f"Replanning (restart {self.restart_count + 1}/{self.max_restarts})",
        )

        notes = self.notes
        if notes is None:
            notes = PipelineNotes(Path(tempfile.mkdtemp(prefix="inkwell-notes-")))

        strategy = await plan_restart(
            self.snapshot,
            notes,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        self.restart_count += 1
        self.snapshot.generation += 1

        await self.execute_strategy(strategy)

    async def execute_strategy(self, strategy: RestartStrategy) -> None:
        """Execute the orchestrator's restart strategy."""
        if strategy.new_plan:
            self.snapshot.plan = strategy.new_plan
            await self.write_plan_tab(strategy.new_plan)
            await self.create_section_tabs(strategy.new_plan)

        rewrite_tasks: list[tuple[SectionPlan, list[str]]] = []
        add_tasks: list[SectionPlan] = []

        for action in strategy.actions:
            match action:
                case PreserveAction():
                    pass
                case PatchAction(section=section, target_text=target, instruction=instr):
                    await self.patch_section(section, target, instr)
                case RewriteAction(section=section, new_research_questions=questions):
                    section_plan = self.find_section_plan(section)
                    if section_plan:
                        rewrite_tasks.append((section_plan, questions))
                case AddAction(section_plan=section_plan):
                    add_tasks.append(section_plan)
                case DropAction(section=section):
                    self.snapshot.section_drafts.pop(section, None)

        if rewrite_tasks or add_tasks:
            await self.execute_rewrites(rewrite_tasks, add_tasks)

        if strategy.needs_remerge and self.snapshot.section_drafts:
            await self.stage_merge()

    async def patch_section(
        self, section: str, target_text: str, instruction: str
    ) -> None:
        """Patch a single section's draft with targeted changes."""
        draft = self.snapshot.section_drafts.get(section)
        if draft is None:
            logger.warning("Cannot patch missing section: %s", section)
            return

        task = (
            f"Apply this targeted edit to the section.\n\n"
            f"## Section: {section}\n\n{draft.content}\n\n"
            f"## Edit Target\n\n\"{target_text}\"\n\n"
            f"## Instruction\n\n{instruction}\n\n"
            f"Return the full updated section in the 'content' field."
        )
        patched = await query(
            task,
            output_type=SectionDraft,
            model="claude-opus-4-6",
            system_prompt=SECTION_WRITER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            permission_mode="bypassPermissions",
            prefix=f"[patch:{section}] ",
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )
        if patched:
            self.snapshot.section_drafts[section] = patched

    async def execute_rewrites(
        self,
        rewrite_tasks: list[tuple[SectionPlan, list[str]]],
        add_tasks: list[SectionPlan],
    ) -> None:
        """Execute rewrites and new sections in parallel."""
        plan = self.snapshot.plan
        research = self.snapshot.research
        if plan is None or research is None:
            return

        all_plans: list[SectionPlan] = []
        all_coros = []
        for section_plan, _questions in rewrite_tasks:
            all_plans.append(section_plan)
            all_coros.append(
                write_section(
                    section_plan,
                    research,
                    plan,
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    trace_logger=self.trace_logger,

                    cost_accumulator=self.cost_accumulator,
                )
            )
        for section_plan in add_tasks:
            all_plans.append(section_plan)
            all_coros.append(
                write_section(
                    section_plan,
                    research,
                    plan,
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    trace_logger=self.trace_logger,

                    cost_accumulator=self.cost_accumulator,
                )
            )

        results = await asyncio.gather(*all_coros, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error("Rewrite/add failed: %s", result)
            else:
                result.title = all_plans[i].title
                self.snapshot.section_drafts[all_plans[i].title] = result

    def find_section_plan(self, section_title: str) -> SectionPlan | None:
        """Look up a SectionPlan by title from the current plan."""
        plan = self.snapshot.plan
        if plan is None:
            return None
        for s in plan.sections:
            if s.title == section_title:
                return s
        return None

    # -- Debounced standby loop ------------------------------------------

    async def standby_loop(
        self, initial_wait: float = 120.0, quiet_wait: float = 90.0
    ) -> None:
        """Post-pipeline revision loop with debounced batching.

        After the pipeline completes, waits for feedback to accumulate
        before triggering a revision. Uses two timers:
        - initial_wait: seconds to wait after first feedback arrives
        - quiet_wait: seconds of silence before considering the batch complete

        This prevents revising after every single comment — instead, the
        author's burst of comments is collected and handled as one revision.

        Plan-breaking comments (classified by the watcher, which is still
        running) route through the orchestrator for section-level decisions.
        Stage-local feedback takes the lightweight rewrite path.
        """
        while True:
            revision = await self.hooks.collect_revision(self.state)
            if revision is None:
                break

            batch = [revision]
            try:
                await asyncio.wait_for(
                    self.drain_feedback_batch(batch, quiet_wait),
                    timeout=initial_wait,
                )
            except asyncio.TimeoutError:
                pass

            combined = " | ".join(batch)
            await self.hooks.on_stage("revise", f"Revising: {combined[:60]}")
            self.state.set_stage("revising")

            self.snapshot.stage = "revise"
            await self.update_overview(active_stage="revise")

            all_feedback = await self.hooks.collect_feedback(self.state)
            all_feedback.extend(batch)
            all_feedback = await self.format_stage_context("revise", all_feedback)

            plan = self.snapshot.plan
            if plan is None:
                break

            has_plan_breaking = (
                self.notes is not None and await self.notes.has_plan_breaking()
            )
            if has_plan_breaking and self.restart_count < self.max_restarts:
                await self.check_and_maybe_restart()
                if self.notes is not None:
                    await self.notes.clear_plan_breaking()

            await self.do_standby_rewrite(all_feedback)

    async def do_standby_rewrite(self, feedback: list[str]) -> None:
        """Execute a lightweight rewrite from standby feedback."""
        plan = self.snapshot.plan
        if plan is None:
            return

        current_content = ""
        if self.final_tab_id:
            try:
                current_content = await do_read_tab(
                    self.doc_id, self.final_tab_id
                )
            except (RuntimeError, OSError):
                logger.warning("Could not read Final tab; using last known")

        if not current_content and self.snapshot.output:
            current_content = self.snapshot.output.content or self.snapshot.output.summary

        output = await rewrite_final(
            current_content,
            self.snapshot.findings,
            plan,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback,
            target_format=self.target_format,
            trace_logger=self.trace_logger,

            cost_accumulator=self.cost_accumulator,
        )

        article_text = output.content or output.summary
        final_content = await apply_format(
            article_text, plan.title, self.target_format
        )
        if self.final_tab_id:
            await do_write_tab(
                self.doc_id, self.final_tab_id, final_content,
                session_state=self.state,
            )
        output.google_doc_id = self.doc_id
        output.google_doc_url = self.doc_url
        output.voice_profile = self.snapshot.voice_profile
        output.word_count = len(final_content.split())  # claude: ignore
        self.snapshot.output = output
        await self.update_overview()
        await self.hooks.on_complete(output)

    async def drain_feedback_batch(
        self, batch: list[str], quiet_seconds: float
    ) -> None:
        """Drain additional feedback until quiet_seconds of silence."""
        while True:
            try:
                extra = await asyncio.wait_for(
                    self.hooks.collect_revision(self.state),
                    timeout=quiet_seconds,
                )
            except asyncio.TimeoutError:
                return
            if extra is None:
                return
            batch.append(extra)

    # -- Notes integration -----------------------------------------------

    async def format_stage_context(
        self, stage: str, feedback: list[str], section: str | None = None
    ) -> list[str]:
        """Build context-aware feedback for a pipeline stage.

        Different stages get different levels of detail from PipelineNotes:
        - research: research notes + direction changes
        - write: section-specific feedback + relevant comments
        - merge: all direction + full comment list
        - review/rewrite: everything
        """
        if self.notes is None:
            return feedback

        match stage:
            case "research":
                comments = await self.notes.list_comments(impact="plan_breaking")
                comments.extend(await self.notes.list_comments(impact="stage_local"))
                if comments:
                    lines = ["## Author Direction (from comments)", ""]
                    for c in comments:
                        line = f"- [{c.impact}] {c.content}"
                        if c.anchor_text:
                            line += f' (on: "{c.anchor_text}")'
                        lines.append(line)
                    feedback = [*feedback, "\n".join(lines)]

            case "write":
                if section:
                    section_fb = await self.notes.get_section_feedback(section)
                    if section_fb:
                        feedback = [*feedback, section_fb]
                else:
                    notes_text = await self.notes.get_all_feedback()
                    if notes_text:
                        feedback = [*feedback, notes_text]

            case "merge" | "review" | "rewrite" | "revise":
                notes_text = await self.notes.get_all_feedback()
                if notes_text:
                    feedback = [*feedback, notes_text]

            case _:
                notes_text = await self.notes.get_all_feedback()
                if notes_text:
                    feedback = [*feedback, notes_text]

        return feedback

    # -- Tab helpers ------------------------------------------------------

    async def write_plan_tab(self, plan: ArticlePlan) -> None:
        """Write the plan to the Plan tab."""
        plan_text = (
            f"# {plan.title}\n\n"
            f"**Thesis:** {plan.thesis}\n\n"
            f"**Author direction:** {plan.author_direction}\n\n"
            f"## Sections\n\n"
            + "\n".join(
                f"### {s.title}\n{s.summary}\n\n"
                f"Key points: {', '.join(s.key_points)}"
                for s in plan.sections
            )
            + "\n\n## Research Questions\n\n"
            + "\n".join(
                f"- [{q.priority}] {q.question} (for: {q.section})"
                for q in plan.research_questions
            )
        )
        if plan.source_quotes:
            plan_text += "\n\n## Source Quotes\n\n" + "\n".join(
                f'- "{q.text}" — {q.speaker}' for q in plan.source_quotes
            )
        plan_tab_id = self.known_tabs["Plan"]
        await do_write_tab(
            self.doc_id, plan_tab_id, plan_text, session_state=self.state
        )
        assert self.tabs is not None
        # Plan tab is reference — don't track for edits

    async def create_section_tabs(self, plan: ArticlePlan) -> None:
        """Create GDoc tabs for each section, nested under a Sections parent."""
        if not self.existing_doc_id:
            await do_rename_doc(self.doc_id, plan.title)

        if "Sections" not in self.known_tabs:
            sections_parent_id = await do_create_tab(
                self.doc_id, "Sections", session_state=self.state
            )
            self.known_tabs["Sections"] = sections_parent_id
        sections_parent_id = self.known_tabs["Sections"]

        self.tab_ids = {}
        for i, section in enumerate(plan.sections):
            tab_name = f"§{i + 1} {section.title}"
            if tab_name in self.known_tabs:
                self.tab_ids[section.title] = self.known_tabs[tab_name]
                self.state.add_section(tab_name, self.known_tabs[tab_name])
            else:
                tid = await do_create_tab(
                    self.doc_id, tab_name,
                    parent_tab_id=sections_parent_id,
                    session_state=self.state,
                )
                self.tab_ids[section.title] = tid
                self.known_tabs[tab_name] = tid

        sections_summary = "\n".join(
            f"- §{i + 1} {s.title}" for i, s in enumerate(plan.sections)
        )
        await do_write_tab(
            self.doc_id, sections_parent_id,
            f"# Sections ({len(plan.sections)})\n\n{sections_summary}",
            session_state=self.state,
        )

    async def write_research_tab(self, research: ResearchCompilation) -> None:
        """Write research findings to the Research tab."""
        research_text = f"# Research Findings\n\n{len(research.findings)} findings\n"
        for finding in research.findings:
            sources_str = (
                ", ".join(s.url for s in finding.sources) if finding.sources else ""
            )
            research_text += (
                f"\n## {finding.question}\n\n"
                f"{finding.answer}\n\n"
                f"**Confidence:** {finding.confidence}"
            )
            if sources_str:
                research_text += f"\n\n**Sources:** {sources_str}"
            research_text += "\n"
        research_tab_id = self.known_tabs["Research"]
        await do_write_tab(
            self.doc_id, research_tab_id, research_text, session_state=self.state
        )
        assert self.tabs is not None
        # Research tab is reference — don't track for edits

    async def write_review_tab(self, findings: list[ReviewFinding]) -> None:
        """Write review findings to the Review tab and post critical as comments."""
        if "Review" not in self.known_tabs:
            self.known_tabs["Review"] = await do_create_tab(
                self.doc_id, "Review", session_state=self.state
            )

        review_text = f"# Review Findings\n\n{len(findings)} total findings\n"
        for finding in findings:
            review_text += (
                f"\n## [{finding.reviewer}] {finding.location}\n\n"
                f"**Severity:** {finding.severity}\n\n"
                f"{finding.issue}\n\n"
                f"**Suggestion:** {finding.suggestion}\n"
            )
            if finding.severity == "critical":
                anchor = finding.text_excerpt if finding.text_excerpt else None
                await do_insert_comment(
                    self.doc_id,
                    f"[{finding.reviewer.upper()}] {finding.issue}\n\n"
                    f"Suggestion: {finding.suggestion}",
                    anchor_text=anchor,
                    session_state=self.state,
                )
        await do_write_tab(
            self.doc_id,
            self.known_tabs["Review"],
            review_text,
            session_state=self.state,
        )

    async def update_overview(self, active_stage: str | None = None) -> None:
        """Write a stage-aware dashboard to the Overview tab.

        active_stage: if set, this stage is currently running (shown as [>]).
            When None, snapshot.stage is treated as completed (shown as [x]).
        """
        snap = self.snapshot
        plan = snap.plan
        completed_stage = snap.stage

        parts: list[str] = [f"# {plan.title if plan else 'Inkwell Pipeline'}\n"]

        all_stages = [
            "extract", "voice", "plan", "research", "assumptions",
            "refine", "write", "merge", "review", "rewrite", "format",
        ]
        completed_idx = all_stages.index(completed_stage) if completed_stage in all_stages else -1
        active_idx = all_stages.index(active_stage) if active_stage and active_stage in all_stages else -1
        progress: list[str] = []
        for i, s in enumerate(all_stages):
            if i == active_idx:
                progress.append(f"[>] {s}")
            elif i <= completed_idx:
                progress.append(f"[x] {s}")
            else:
                progress.append(f"[ ] {s}")
        parts.append(" ".join(progress) + "\n")

        if plan:
            parts.append(f"**Thesis:** {plan.thesis}\n")

        if plan and snap.research:
            answered = len(snap.research.findings)
            total = len(plan.research_questions)
            parts.append(f"**Research:** {answered}/{total} questions answered\n")
        elif plan:
            parts.append(f"**Research:** {len(plan.research_questions)} questions queued\n")

        if plan:
            parts.append("\n## Sections\n")
            for i, s in enumerate(plan.sections):
                draft = snap.section_drafts.get(s.title)
                if draft and not draft.content.startswith("[Section failed"):
                    parts.append(f"- [x] {s.title} ({draft.word_count} words)")
                elif active_stage == "write" and s.title not in snap.section_drafts:
                    parts.append(f"- [>] {s.title}")
                else:
                    parts.append(f"- [ ] {s.title}")
            total_words = sum(d.word_count for d in snap.section_drafts.values())
            if total_words:
                parts.append(
                    f"\n**Total:** {total_words} words "
                    f"across {len(snap.section_drafts)} sections"
                )

        if snap.findings:
            critical = sum(1 for f in snap.findings if f.severity == "critical")
            suggestions = sum(1 for f in snap.findings if f.severity == "suggestion")
            parts.append(f"\n## Review\n\n{critical} critical, {suggestions} suggestions")

        if self.state.pending_questions:
            n = len(self.state.pending_questions)
            parts.append(f"\n**Questions for author:** {n} pending")

        if active_stage == "revise":
            parts.append("\n*Processing your feedback...*")
        else:
            parts.append("\n*Comment on any tab to give feedback.*")

        await do_write_tab(
            self.doc_id, self.overview_tab_id,
            "\n".join(parts), session_state=self.state,
        )


async def run_pipeline(
    source: str,
    *,
    target_format: str = "lesswrong",
    existing_doc_id: str | None = None,
    session_state: WritingSessionState | None = None,
    notes: PipelineNotes | None = None,
    trace_logger: TraceLogger | None = None,
    refs: list[str] | None = None,
    listener: PipelineListener | None = None,

    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Run the complete writing pipeline.

    Thin wrapper around PipelineRunner for backward compatibility.
    """
    runner = PipelineRunner(
        source,
        target_format=target_format,
        existing_doc_id=existing_doc_id,
        session_state=session_state,
        notes=notes,
        trace_logger=trace_logger,
        refs=refs,
        listener=listener,

        cost_accumulator=cost_accumulator,
    )
    return await runner.run()
