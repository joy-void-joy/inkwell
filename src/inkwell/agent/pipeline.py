"""Unified writing pipeline with restartable state machine.

Both interactive and batch modes use this pipeline. The only difference
is the PipelineListener: batch mode logs to stdout, interactive mode
shows Rich progress and accepts terminal input.

Stages:
  extract -> voice -> plan -> assumptions -> research -> write -> merge -> review -> rewrite -> format

The PipelineRunner wraps stages in a state machine that:
- Accumulates artifacts in PipelineSnapshot
- Checks for plan-breaking author feedback between stages
- Runs the orchestrator to produce fine-grained RestartStrategy
- Executes restart actions (preserve/patch/rewrite/add/drop per section)
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false
# Google API service objects are untyped.

import asyncio
import logging
import tempfile
from pathlib import Path

from claude_agent_sdk import McpServerConfig

from lup.client import CostAccumulator, query
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
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    SECTION_WRITER_PROMPT,
    STYLE_REVIEWER_PROMPT,
)
from inkwell.agent.tool_policy import research_tool_names, review_tool_names
from inkwell.agent.tools.extract import do_extract_source
from inkwell.agent.tools.formats import (
    FormatBlogInput,
    FormatLesswrongInput,
    FormatTwitterInput,
    do_format_blog,
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


class BudgetTracker:
    """Tracks cumulative pipeline spend and computes remaining budget."""

    def __init__(
        self, max_budget_usd: float | None, accumulator: CostAccumulator
    ) -> None:
        self.max_budget_usd = max_budget_usd
        self.accumulator = accumulator

    def remaining(self) -> float | None:
        if self.max_budget_usd is None:
            return None
        left = self.max_budget_usd - self.accumulator.total_cost_usd
        return max(left, 0.0)


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
            original_stripped = original.strip()
            current_stripped = current.strip()
            if current_stripped != original_stripped and current_stripped:
                edits.append(
                    f"[GDoc Edit: {label}] Author modified this tab. "
                    f"Current content:\n{current_stripped}"
                )
        return edits


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
    return f"\n\n## Author Feedback\n\n{lines}\n"


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
    max_budget_usd: float | None = None,
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

    task = (
        f"Extract a structured article plan from this conversation.\n"
        f"Target format: {target_format}\n\n"
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
        max_turns=1,
        prefix="[plan] ",
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
        cost_accumulator=cost_accumulator,
    )
    if plan is None:
        raise PipelineError("Planner produced no structured output")
    return plan


async def research_plan(
    plan: ArticlePlan,
    *,
    feedback: list[str] | None = None,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,
    max_budget_usd: float | None = None,
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
        f"## Research Questions\n{questions_text}\n\n"
        f"## Source Quotes (for awareness)\n{quotes_text}\n\n"
        f"Be thorough. Cross-reference claims. Prefer primary sources."
        f"{format_feedback(feedback or [])}"
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
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
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
        f"## Research Findings\n{findings_text}\n\n"
        f"## Quotes to Weave In\n{quotes_text or '(none)'}\n\n"
        f"{voice_section}\n\n"
        f"## Author Direction\n{plan.author_direction}\n\n"
        f"Return the section content as markdown in the 'content' field."
        f"{format_feedback(feedback or [])}"
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
        max_budget_usd=max_budget_usd,
        cost_accumulator=cost_accumulator,
    )
    if draft is None:
        raise PipelineError(
            f"Section writer for '{section_plan.title}' produced no output"
        )
    return draft


async def write_all_sections(
    plan: ArticlePlan,
    research: ResearchCompilation,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    servers: dict[str, McpServerConfig] | None = None,
    trace_logger: TraceLogger | None = None,
    max_budget_usd: float | None = None,
    cost_accumulator: CostAccumulator | None = None,
    listener: PipelineListener | None = None,
) -> list[SectionDraft]:
    """Stage 3: Write all sections in parallel."""
    if servers is None:
        servers = build_research_servers()

    tasks = [
        write_section(
            section,
            research,
            plan,
            voice_profile=voice_profile,
            feedback=feedback,
            servers=servers,
            trace_logger=trace_logger,
            max_budget_usd=max_budget_usd,
            cost_accumulator=cost_accumulator,
        )
        for section in plan.sections
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    drafts: list[SectionDraft] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            logger.error("Section '%s' failed: %s", plan.sections[i].title, result)
            drafts.append(
                SectionDraft(
                    title=plan.sections[i].title,
                    content=f"[Section failed: {result}]",
                    word_count=0,
                )
            )
        else:
            drafts.append(result)
            if listener:
                await listener.on_message(
                    "write",
                    f"Section '{result.title}' complete ({result.word_count} words)",
                )
    return drafts


async def merge_sections(
    drafts: list[SectionDraft],
    plan: ArticlePlan,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    max_budget_usd: float | None = None,
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
        f"Merge these independently-written sections into a coherent draft "
        f'for the article "{plan.title}".\n\n'
        f"Thesis: {plan.thesis}\n\n"
        f"## Sections (in order)\n\n{sections_text}\n\n"
        f"Fix transitions, remove redundancy, normalize depth. "
        f"The draft should read as if one person wrote it in one sitting. "
        f"Return the complete merged text in the 'content' field."
        f"{voice_section}"
        f"{format_feedback(feedback or [])}"
    )

    merged = await query(
        task,
        output_type=MergedDraft,
        model="claude-opus-4-6",
        system_prompt=COHERENCE_EDITOR_PROMPT,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        max_turns=1,
        trace_logger=trace_logger,
        prefix="[merge] ",
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
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
        max_turns=1,
        trace_logger=trace_logger,
        prefix="[review:narrative] ",
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
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
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
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
        max_turns=1,
        trace_logger=trace_logger,
        prefix="[review:style] ",
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[ReviewFinding]:
    """Stage 5: Run all three reviewers in parallel."""
    narrative_task = review_narrative(
        draft,
        plan,
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
        cost_accumulator=cost_accumulator,
    )
    facts_task = review_facts(
        draft,
        plan,
        servers=servers,
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
        cost_accumulator=cost_accumulator,
    )
    style_task = review_style(
        draft,
        plan,
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
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
    max_budget_usd: float | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Stage 6: Incorporate all feedback and produce the final article."""
    findings_by_severity = {
        "critical": [f for f in findings if f.severity == "critical"],
        "suggestion": [f for f in findings if f.severity == "suggestion"],
        "praise": [f for f in findings if f.severity == "praise"],
    }

    findings_text = ""
    for severity in ("critical", "suggestion"):
        items = findings_by_severity[severity]
        if items:
            findings_text += f"\n### {severity.title()} ({len(items)})\n"
            for f in items:
                findings_text += (
                    f"- [{f.reviewer}] {f.location}: {f.issue}\n"
                    f"  Suggestion: {f.suggestion}\n"
                )

    voice_section = ""
    if voice_profile:
        voice_section = format_voice_profile(voice_profile)

    task = (
        f'Produce the final version of "{plan.title}".\n\n'
        f"Target format: {target_format}\n\n"
        f"## Draft\n\n{draft}\n\n"
        f"## Review Findings\n{findings_text or '(no findings)'}\n\n"
        f"## Author Direction\n{plan.author_direction}\n\n"
        f"Apply all critical fixes. Consider suggestions. "
        f"Return the full article text in the 'content' field "
        f"and a brief 1-2 sentence editorial summary in the 'summary' field."
        f"{voice_section}"
        f"{format_feedback(feedback or [])}"
    )

    output = await query(
        task,
        output_type=WritingOutput,
        model="claude-opus-4-6",
        system_prompt=REWRITER_SYSTEM,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        max_turns=1,
        trace_logger=trace_logger,
        prefix="[rewrite] ",
        max_budget_usd=max_budget_usd,
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
        case _:
            return content


# ---------------------------------------------------------------------------
# New stages (assumptions, orchestrator)
# ---------------------------------------------------------------------------


async def surface_assumptions(
    plan: ArticlePlan,
    doc_id: str,
    *,
    session_state: WritingSessionState,
    trace_logger: TraceLogger | None = None,
    max_budget_usd: float | None = None,
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

    result = await query(
        task,
        output_type=AssumptionsList,
        model="claude-sonnet-4-20250514",
        system_prompt=ASSUMPTIONS_PROMPT,
        permission_mode="bypassPermissions",
        max_turns=1,
        prefix="[assumptions] ",
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
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
            f"My best guess: {item.best_guess}\n\n"
            f"(re: {item.anchor_section})"
        )
        await do_insert_comment(doc_id, comment_text, session_state=session_state)

    return result


async def plan_restart(
    snapshot: PipelineSnapshot,
    notes: PipelineNotes,
    *,
    trace_logger: TraceLogger | None = None,
    max_budget_usd: float | None = None,
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
        max_turns=1,
        prefix="[orchestrator] ",
        trace_logger=trace_logger,
        max_budget_usd=max_budget_usd,
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
        max_budget_usd: float | None = None,
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
        self.budget = BudgetTracker(max_budget_usd, cost_accumulator)

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

    async def run(self) -> WritingOutput:
        """Execute the full pipeline with restart support."""
        configure_session_state(self.state)
        await self.setup_doc()

        await self.stage_extract()
        await self.stage_voice()
        await self.stage_plan()
        await self.stage_assumptions()

        self.start_watcher()

        try:
            await self.stage_research()
            await self.check_and_maybe_restart()

            await self.stage_write()
            await self.check_and_maybe_restart()

            await self.stage_merge()
            await self.stage_review()
            await self.check_and_maybe_restart()

            await self.stage_rewrite()
            await self.stage_format()
        finally:
            await self.stop_watcher()

        output = self.snapshot.output
        if output is None:
            raise PipelineError("Pipeline completed without producing output")

        await self.update_overview_complete()
        await self.hooks.on_complete(output)

        await self.standby_loop()

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
        conversation = await do_extract_source(self.source)

        for ref in self.refs:
            try:
                ref_text = await do_extract_source(ref)
                conversation += f"\n\n--- Reference: {ref} ---\n\n{ref_text}"
            except RuntimeError:
                logger.warning("Failed to extract reference: %s", ref)

        self.snapshot.conversation = conversation
        self.snapshot.stage = "extract"

        source_tab_id = self.known_tabs["Source"]
        await do_write_tab(
            self.doc_id, source_tab_id, conversation, session_state=self.state
        )
        assert self.tabs is not None
        self.tabs.record(source_tab_id, "Source", conversation)

    async def stage_voice(self) -> None:
        """Analyze the author's writing voice."""
        await self.hooks.on_stage("voice", "Analyzing author's writing voice")
        corpus_samples, _ = load_style_corpus(max_samples=3)
        voice_profile = await do_analyze_voice(
            self.snapshot.conversation,
            corpus_samples,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.voice_profile = voice_profile
        self.snapshot.stage = "voice"

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
            self.tabs.record(voice_tab_id, "Voice", voice_text)

    async def stage_plan(self) -> None:
        """Plan the article structure."""
        await self.hooks.on_stage("plan", "Planning article structure")
        plan = await plan_article(
            self.snapshot.conversation,
            target_format=self.target_format,
            voice_profile=self.snapshot.voice_profile,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.plan = plan
        self.snapshot.stage = "plan"

        await self.hooks.on_progress(
            f"Plan: '{plan.title}' — {len(plan.sections)} sections, "
            f"{len(plan.research_questions)} research questions"
        )
        await self.write_plan_tab(plan)
        await self.create_section_tabs(plan)

    async def stage_assumptions(self) -> None:
        """Surface uncertainties as GDoc comments (non-blocking)."""
        plan = self.snapshot.plan
        if plan is None:
            return
        await self.hooks.on_stage("assumptions", "Surfacing questions for the author")
        assumptions = await surface_assumptions(
            plan,
            self.doc_id,
            session_state=self.state,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.stage = "assumptions"
        if assumptions.items:
            await self.hooks.on_progress(
                f"Posted {len(assumptions.items)} questions/assumptions as GDoc comments"
            )

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
        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("research", feedback)

        research = await research_plan(
            plan,
            feedback=feedback_with_notes,
            servers=self.research_servers,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.research = research
        self.snapshot.stage = "research"

        await self.hooks.on_progress(
            f"Research complete: {len(research.findings)} findings"
        )
        await self.write_research_tab(research)

    async def stage_write(self) -> None:
        """Write all sections in parallel."""
        plan = self.snapshot.plan
        research = self.snapshot.research
        if plan is None or research is None:
            raise PipelineError("Cannot write without plan and research")

        await self.hooks.on_stage(
            "write",
            f"Writing {len(plan.sections)} sections in parallel",
        )
        self.state.set_stage("writing")
        await self.update_overview("writing", plan)

        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("write", feedback)

        drafts = await write_all_sections(
            plan,
            research,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback_with_notes,
            servers=self.research_servers,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
            listener=self.hooks,
        )

        assert self.tabs is not None
        for draft in drafts:
            self.snapshot.section_drafts[draft.title] = draft
            tid = self.tab_ids.get(draft.title, "")
            if tid and draft.content and not draft.content.startswith("[Section failed"):
                await do_write_tab(
                    self.doc_id, tid, draft.content, session_state=self.state
                )
                self.tabs.record(tid, f"Section: {draft.title}", draft.content)
                self.state.update_section_status(draft.title, "drafted")

            for question in draft.questions_for_author:
                await do_insert_comment(
                    self.doc_id,
                    f"[QUESTION] {question}",
                    session_state=self.state,
                )
                self.state.add_question(question)

        self.snapshot.stage = "write"

    async def stage_merge(self) -> None:
        """Merge sections into a coherent draft."""
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot merge without a plan")

        await self.hooks.on_stage("merge", "Merging sections into coherent draft")
        self.state.set_stage("merging")
        feedback = await self.gather_feedback()
        feedback_with_notes = await self.format_stage_context("merge", feedback)

        drafts = list(self.snapshot.section_drafts.values())
        merged = await merge_sections(
            drafts,
            plan,
            voice_profile=self.snapshot.voice_profile,
            feedback=feedback_with_notes,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.merged = merged
        self.snapshot.stage = "merge"

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

    async def stage_review(self) -> None:
        """Run parallel reviewers on the merged draft."""
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot review without plan and merged draft")

        await self.hooks.on_stage("review", "Reviewing draft (3 reviewers in parallel)")
        self.state.set_stage("reviewing")
        await self.update_overview("reviewing", plan)

        findings = await review_all(
            merged.content,
            plan,
            servers=self.research_servers,
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        self.snapshot.findings = findings
        self.snapshot.stage = "review"

        await self.hooks.on_progress(
            f"Review complete: {len(findings)} findings "
            f"({sum(1 for f in findings if f.severity == 'critical')} critical)"
        )
        await self.write_review_tab(findings)

    async def stage_rewrite(self) -> None:
        """Produce the final version incorporating review feedback."""
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot rewrite without plan and merged draft")

        await self.hooks.on_stage("rewrite", "Producing final version")
        self.state.set_stage("rewriting")
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
            max_budget_usd=self.budget.remaining(),
            cost_accumulator=self.cost_accumulator,
        )
        output.voice_profile = self.snapshot.voice_profile
        output.open_questions = list(self.state.pending_questions)
        self.snapshot.output = output
        self.snapshot.stage = "rewrite"

    async def stage_format(self) -> None:
        """Apply format-specific transformations and write to Final tab."""
        output = self.snapshot.output
        plan = self.snapshot.plan
        if output is None or plan is None:
            raise PipelineError("Cannot format without output and plan")

        await self.hooks.on_stage("format", f"Applying {self.target_format} formatting")
        article_text = output.content or output.summary
        final_content = await apply_format(
            article_text, plan.title, self.target_format
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
        output.sections_completed = sum(
            1 for d in self.snapshot.section_drafts.values()
            if not d.content.startswith("[Section failed")
        )
        output.word_count = len(final_content.split())  # claude: ignore
        self.state.set_stage("complete")

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
            max_budget_usd=self.budget.remaining(),
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
            model="claude-sonnet-4-20250514",
            system_prompt=SECTION_WRITER_PROMPT,
            permission_mode="bypassPermissions",
            max_turns=1,
            prefix=f"[patch:{section}] ",
            trace_logger=self.trace_logger,
            max_budget_usd=self.budget.remaining(),
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

        all_coros = []
        for section_plan, _questions in rewrite_tasks:
            all_coros.append(
                write_section(
                    section_plan,
                    research,
                    plan,
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    trace_logger=self.trace_logger,
                    max_budget_usd=self.budget.remaining(),
                    cost_accumulator=self.cost_accumulator,
                )
            )
        for section_plan in add_tasks:
            all_coros.append(
                write_section(
                    section_plan,
                    research,
                    plan,
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    trace_logger=self.trace_logger,
                    max_budget_usd=self.budget.remaining(),
                    cost_accumulator=self.cost_accumulator,
                )
            )

        results = await asyncio.gather(*all_coros, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("Rewrite/add failed: %s", result)
            else:
                self.snapshot.section_drafts[result.title] = result

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

            all_feedback = await self.hooks.collect_feedback(self.state)
            all_feedback.extend(batch)
            all_feedback = await self.format_stage_context("revise", all_feedback)

            plan = self.snapshot.plan
            if plan is None:
                break

            output = await rewrite_final(
                current_content,
                self.snapshot.findings,
                plan,
                voice_profile=self.snapshot.voice_profile,
                feedback=all_feedback,
                target_format=self.target_format,
                trace_logger=self.trace_logger,
                max_budget_usd=self.budget.remaining(),
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
        self.tabs.record(plan_tab_id, "Plan", plan_text)

    async def create_section_tabs(self, plan: ArticlePlan) -> None:
        """Create GDoc tabs for each section in the plan."""
        if not self.existing_doc_id:
            await do_rename_doc(self.doc_id, plan.title)

        self.tab_ids = {}
        for section in plan.sections:
            tab_name = f"§{len(self.tab_ids) + 1} {section.title}"
            if tab_name in self.known_tabs:
                self.tab_ids[section.title] = self.known_tabs[tab_name]
                self.state.add_section(tab_name, self.known_tabs[tab_name])
            else:
                tid = await do_create_tab(
                    self.doc_id, tab_name, session_state=self.state
                )
                self.tab_ids[section.title] = tid
                self.known_tabs[tab_name] = tid

        await self.update_overview("researching", plan)

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
        self.tabs.record(research_tab_id, "Research", research_text)

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

    async def update_overview(self, stage: str, plan: ArticlePlan) -> None:
        """Update the overview tab with current stage."""
        marker = {"researching": "[ ]", "writing": "[>]", "reviewing": "[~]"}.get(
            stage, "[ ]"
        )
        await do_write_tab(
            self.doc_id,
            self.overview_tab_id,
            (
                f"# {plan.title}\n\n"
                f"**Thesis:** {plan.thesis}\n\n"
                f"**Stage:** {stage}\n\n"
                f"## Sections\n\n"
                + "\n".join(f"- {marker} {s.title}" for s in plan.sections)
            ),
            session_state=self.state,
        )

    async def update_overview_complete(self) -> None:
        """Write the final overview when pipeline completes."""
        plan = self.snapshot.plan
        findings = self.snapshot.findings
        if plan is None:
            return

        await do_write_tab(
            self.doc_id,
            self.overview_tab_id,
            (
                f"# {plan.title}\n\n"
                f"**Stage:** complete\n\n"
                f"## Tabs\n\n"
                f"- **Source** — extracted conversation\n"
                f"- **Voice** — detected writing style\n"
                f"- **Plan** — article structure\n"
                f"- **Research** — findings and sources\n"
                + "".join(
                    f"- **§{i+1} {s.title}** — section draft\n"
                    for i, s in enumerate(plan.sections)
                )
                + f"- **Draft** — merged coherent draft\n"
                f"- **Review** — all reviewer findings\n"
                f"- **Final** — formatted output\n\n"
                f"## Review Summary\n\n"
                f"- {len(findings)} findings total\n"
                f"- {sum(1 for f in findings if f.severity == 'critical')} critical\n"
                f"- {sum(1 for f in findings if f.severity == 'suggestion')} suggestions\n\n"
                f"*Comment on any tab or the Final to request revisions.*"
            ),
            session_state=self.state,
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
    max_budget_usd: float | None = None,
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
        max_budget_usd=max_budget_usd,
        cost_accumulator=cost_accumulator,
    )
    return await runner.run()
