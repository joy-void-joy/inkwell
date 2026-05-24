"""Unified writing pipeline.

Both interactive and batch modes use this pipeline. The only difference
is the PipelineListener: batch mode logs to stdout, interactive mode
shows Rich progress and accepts terminal input.

Stages:
  extract -> plan -> voice -> research -> write sections -> merge -> review -> rewrite -> format
"""

# claude: ignore
# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false
# Google API service objects are untyped.

import asyncio
import logging

from claude_agent_sdk import McpServerConfig

from lup.client import CostAccumulator, query
from lup.mcp import LupMcpTool, create_mcp_server, extract_sdk_tools
from lup.trace import TraceLogger

from inkwell.agent.config import settings
from inkwell.agent.models import (
    ArticlePlan,
    MergedDraft,
    ResearchCompilation,
    ReviewFinding,
    ReviewOutput,
    SectionDraft,
    SectionPlan,
    WritingOutput,
)
from inkwell.agent.session import WritingSessionState
from inkwell.agent.stages import (
    COHERENCE_EDITOR_PROMPT,
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
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
    """Format author feedback for inclusion in a stage's task prompt."""
    if not feedback:
        return ""
    lines = "\n".join(f"- {f}" for f in feedback)
    return f"\n\n## Author Feedback\n\n{lines}\n"


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
        voice_section += (
            f"\n\n## Voice Profile\n\n"
            f"Formality: {voice_profile.formality}\n"
            f"Sentence rhythm: {voice_profile.sentence_rhythm}\n"
            f"Hedging: {voice_profile.hedging_style}\n"
            f"Humor: {voice_profile.humor}\n"
            f"Technical depth: {voice_profile.technical_depth}\n"
            f"Paragraph style: {voice_profile.paragraph_style}\n"
            f"Argumentation: {voice_profile.argumentation}\n"
            f"Summary: {voice_profile.summary}"
        )

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
        voice_section += (
            f"\n\nVoice profile: {voice_profile.summary}\n"
            f"Formality: {voice_profile.formality}, "
            f"humor: {voice_profile.humor}, "
            f"depth: {voice_profile.technical_depth}"
        )

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
        voice_section = (
            f"\n\n## Voice Profile\n"
            f"Summary: {voice_profile.summary}\n"
            f"Formality: {voice_profile.formality}, "
            f"humor: {voice_profile.humor}, "
            f"depth: {voice_profile.technical_depth}\n"
        )

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
        model="claude-sonnet-4-6",
        system_prompt=NARRATIVE_REVIEWER_PROMPT,
        max_thinking_tokens=16_000,
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
        model="claude-sonnet-4-6",
        system_prompt=STYLE_REVIEWER_PROMPT,
        max_thinking_tokens=16_000,
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
        voice_section = (
            f"\n\n## Voice Profile\n"
            f"Summary: {voice_profile.summary}\n"
            f"Formality: {voice_profile.formality}, "
            f"humor: {voice_profile.humor}, "
            f"depth: {voice_profile.technical_depth}\n"
        )

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
# Pipeline runner
# ---------------------------------------------------------------------------


async def run_pipeline(
    source: str,
    *,
    target_format: str = "lesswrong",
    existing_doc_id: str | None = None,
    session_state: WritingSessionState | None = None,
    trace_logger: TraceLogger | None = None,
    refs: list[str] | None = None,
    listener: PipelineListener | None = None,
    max_budget_usd: float | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Run the complete writing pipeline.

    Both interactive and batch modes call this function. The listener
    receives progress events and collects author feedback at stage
    boundaries. Without a listener, the pipeline logs and continues.
    """
    state = session_state or WritingSessionState()
    configure_session_state(state)
    hooks = listener or PipelineListener()

    if cost_accumulator is None:
        cost_accumulator = CostAccumulator()
    budget = BudgetTracker(max_budget_usd, cost_accumulator)

    research_servers = build_research_servers()

    # ── Stage 1: Extract ──────────────────────────────────────────────
    await hooks.on_stage("extract", "Extracting source material")
    conversation = await do_extract_source(source)

    if refs:
        for ref in refs:
            try:
                ref_text = await do_extract_source(ref)
                conversation += f"\n\n--- Reference: {ref} ---\n\n{ref_text}"
            except RuntimeError:
                logger.warning("Failed to extract reference: %s", ref)

    # ── Voice analysis ────────────────────────────────────────────────
    await hooks.on_stage("voice", "Analyzing author's writing voice")
    corpus_samples, _ = load_style_corpus(max_samples=3)
    voice_profile = await do_analyze_voice(conversation, corpus_samples)
    if voice_profile:
        await hooks.on_progress(
            f"Voice: {voice_profile.formality}, {voice_profile.humor}"
        )

    # ── Stage 2: Plan ─────────────────────────────────────────────────
    await hooks.on_stage("plan", "Planning article structure")
    plan = await plan_article(
        conversation,
        target_format=target_format,
        voice_profile=voice_profile,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
    )
    await hooks.on_progress(
        f"Plan: '{plan.title}' — {len(plan.sections)} sections, "
        f"{len(plan.research_questions)} research questions"
    )

    # ── Create or reuse Google Doc ───────────────────────────────────
    known_tabs: dict[str, str] = {}
    if existing_doc_id:
        doc_id = existing_doc_id
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        state.set_doc(doc_id, doc_url)
        await hooks.on_progress(f"Reusing Google Doc: {doc_url}")

        existing_tabs = await do_list_tabs(doc_id)
        known_tabs = {t.title: t.tab_id for t in existing_tabs}

        overview_tab_id = known_tabs.get("Overview", "")
        if not overview_tab_id:
            overview_tab_id = await do_create_tab(doc_id, "Overview", session_state=state)

        tab_ids: dict[str, str] = {}
        for section in plan.sections:
            tab_name = f"§{len(tab_ids) + 1} {section.title}"
            if tab_name in known_tabs:
                tab_ids[section.title] = known_tabs[tab_name]
                state.add_section(tab_name, known_tabs[tab_name])
            else:
                tid = await do_create_tab(doc_id, tab_name, session_state=state)
                tab_ids[section.title] = tid
    else:
        doc_id, doc_url = await do_create_doc(
            plan.title,
            share_with=settings.author_email,
            session_state=state,
        )
        await hooks.on_progress(f"Google Doc: {doc_url}")

        overview_tab_id = await do_create_tab(doc_id, "Overview", session_state=state)
        tab_ids = {}
        for section in plan.sections:
            tab_name = f"§{len(tab_ids) + 1} {section.title}"
            tid = await do_create_tab(doc_id, tab_name, session_state=state)
            tab_ids[section.title] = tid

    await do_write_tab(
        doc_id,
        overview_tab_id,
        (
            f"# {plan.title}\n\n"
            f"**Thesis:** {plan.thesis}\n\n"
            f"**Stage:** researching\n\n"
            f"## Sections\n\n" + "\n".join(f"- [ ] {s.title}" for s in plan.sections)
        ),
        session_state=state,
    )

    # ── Feedback checkpoint: after plan ───────────────────────────────
    feedback = await hooks.collect_feedback(state)

    # ── Stage 3: Research ─────────────────────────────────────────────
    await hooks.on_stage(
        "research",
        f"Researching {len(plan.research_questions)} questions",
    )
    state.set_stage("researching")
    research = await research_plan(
        plan,
        feedback=feedback,
        servers=research_servers,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
    )
    await hooks.on_progress(f"Research complete: {len(research.findings)} findings")

    # ── Feedback checkpoint: after research ───────────────────────────
    feedback = await hooks.collect_feedback(state)

    # ── Stage 4: Write sections (parallel) ────────────────────────────
    await hooks.on_stage(
        "write",
        f"Writing {len(plan.sections)} sections in parallel",
    )
    state.set_stage("writing")
    await do_write_tab(
        doc_id,
        overview_tab_id,
        (
            f"# {plan.title}\n\n"
            f"**Stage:** writing ({len(plan.sections)} sections in parallel)\n\n"
            f"## Sections\n\n" + "\n".join(f"- [>] {s.title}" for s in plan.sections)
        ),
        session_state=state,
    )

    drafts = await write_all_sections(
        plan,
        research,
        voice_profile=voice_profile,
        feedback=feedback,
        servers=research_servers,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
        listener=hooks,
    )

    for draft in drafts:
        tid = tab_ids.get(draft.title, "")
        if tid and draft.content and not draft.content.startswith("[Section failed"):
            await do_write_tab(doc_id, tid, draft.content, session_state=state)
            state.update_section_status(draft.title, "drafted")

        for question in draft.questions_for_author:
            await do_insert_comment(
                doc_id,
                f"[QUESTION] {question}",
                session_state=state,
            )
            state.add_question(question)

    # ── Feedback checkpoint: after writing ────────────────────────────
    feedback = await hooks.collect_feedback(state)

    # ── Stage 5: Merge ────────────────────────────────────────────────
    await hooks.on_stage("merge", "Merging sections into coherent draft")
    state.set_stage("merging")
    merged = await merge_sections(
        drafts,
        plan,
        voice_profile=voice_profile,
        feedback=feedback,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
    )

    draft_tab_id = known_tabs.get("Draft", "")
    if not draft_tab_id:
        draft_tab_id = await do_create_tab(doc_id, "Draft", session_state=state)
    await do_write_tab(doc_id, draft_tab_id, merged.content, session_state=state)

    # ── Feedback checkpoint: after merge ──────────────────────────────
    feedback = await hooks.collect_feedback(state)

    # ── Stage 6: Review (parallel) ────────────────────────────────────
    await hooks.on_stage("review", "Reviewing draft (3 reviewers in parallel)")
    state.set_stage("reviewing")
    await do_write_tab(
        doc_id,
        overview_tab_id,
        (
            f"# {plan.title}\n\n"
            f"**Stage:** reviewing (narrative + fact-check + style)\n\n"
            f"## Sections\n\n" + "\n".join(f"- [~] {s.title}" for s in plan.sections)
        ),
        session_state=state,
    )

    findings = await review_all(
        merged.content,
        plan,
        servers=research_servers,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
    )
    await hooks.on_progress(
        f"Review complete: {len(findings)} findings "
        f"({sum(1 for f in findings if f.severity == 'critical')} critical)"
    )

    for finding in findings:
        if finding.severity == "critical":
            anchor = finding.text_excerpt if finding.text_excerpt else None
            await do_insert_comment(
                doc_id,
                f"[{finding.reviewer.upper()}] {finding.issue}\n\n"
                f"Suggestion: {finding.suggestion}",
                anchor_text=anchor,
                session_state=state,
            )

    # ── Feedback checkpoint: after review ─────────────────────────────
    feedback = await hooks.collect_feedback(state)

    # ── Stage 7: Rewrite ──────────────────────────────────────────────
    await hooks.on_stage("rewrite", "Producing final version")
    state.set_stage("rewriting")
    output = await rewrite_final(
        merged.content,
        findings,
        plan,
        voice_profile=voice_profile,
        feedback=feedback,
        target_format=target_format,
        trace_logger=trace_logger,
        max_budget_usd=budget.remaining(),
        cost_accumulator=cost_accumulator,
    )

    output.voice_profile = voice_profile
    output.open_questions = list(state.pending_questions)

    # ── Stage 8: Format ───────────────────────────────────────────────
    await hooks.on_stage("format", f"Applying {target_format} formatting")
    article_text = output.content or output.summary
    final_content = await apply_format(article_text, plan.title, target_format)

    final_tab_id = known_tabs.get("Final", "")
    if not final_tab_id:
        final_tab_id = await do_create_tab(doc_id, "Final", session_state=state)
    await do_write_tab(doc_id, final_tab_id, final_content, session_state=state)

    output.google_doc_id = doc_id
    output.google_doc_url = doc_url
    output.sections_completed = sum(
        1 for d in drafts if not d.content.startswith("[Section failed")
    )
    output.word_count = len(final_content.split())  # claude: ignore

    # ── Update overview ───────────────────────────────────────────────
    state.set_stage("complete")
    await do_write_tab(
        doc_id,
        overview_tab_id,
        (
            f"# {plan.title}\n\n"
            f"**Stage:** complete\n\n"
            f"**Final tab:** ready for review\n\n"
            f"## Sections\n\n"
            + "\n".join(f"- [x] {s.title}" for s in plan.sections)
            + f"\n\n## Review Summary\n\n"
            f"- {len(findings)} findings total\n"
            f"- {sum(1 for f in findings if f.severity == 'critical')} critical\n"
            f"- {sum(1 for f in findings if f.severity == 'suggestion')} suggestions\n"
        ),
        session_state=state,
    )

    await hooks.on_complete(output)

    # ── Post-pipeline revision loop ───────────────────────────────────
    while True:
        revision = await hooks.collect_revision(state)
        if revision is None:
            break
        await hooks.on_stage("revise", f"Revising: {revision[:60]}")
        state.set_stage("revising")

        current_content = final_content
        try:
            current_content = await do_read_tab(doc_id, final_tab_id)
        except (RuntimeError, OSError):
            logger.warning("Could not read Final tab; using last known content")

        all_feedback = await hooks.collect_feedback(state)
        all_feedback.insert(0, revision)
        output = await rewrite_final(
            current_content,
            findings,
            plan,
            voice_profile=voice_profile,
            feedback=all_feedback,
            target_format=target_format,
            trace_logger=trace_logger,
            max_budget_usd=budget.remaining(),
            cost_accumulator=cost_accumulator,
        )
        article_text = output.content or output.summary
        final_content = await apply_format(article_text, plan.title, target_format)
        await do_write_tab(doc_id, final_tab_id, final_content, session_state=state)
        output.google_doc_id = doc_id
        output.google_doc_url = doc_url
        output.voice_profile = voice_profile
        output.word_count = len(final_content.split())  # claude: ignore
        await hooks.on_complete(output)

    return output
