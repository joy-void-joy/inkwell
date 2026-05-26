# claude: ignore
# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false
"""Unified writing pipeline with restartable state machine.

Stages read inputs from files (via built-in Read) and produce output
via incremental MCP tools (plan, research, review) or built-in Write/Edit
(prose stages). This keeps context lean — agents pull what they need
rather than receiving everything in the prompt.
"""

import asyncio
import difflib
import json
import logging
import tempfile
import unicodedata
from pathlib import Path

from claude_agent_sdk import McpServerConfig
from pydantic import BaseModel, Field

from lup.client import CostAccumulator, HeartbeatCallback, query
from lup.mcp import LupMcpTool, create_mcp_server, extract_sdk_tools
from lup.trace import TraceLogger

from inkwell.agent.config import settings
from inkwell.agent.models import (
    AddAction,
    ArticlePlan,
    AssumptionsList,
    ClassifiedComment,
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
    COMMENT_CLASSIFIER_PROMPT,
    FACT_CHECKER_PROMPT,
    MERGE_PLAN_PROMPT,
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
    gdoc_nonfatal,
    truncate_tab_title,
)
from inkwell.agent.tools.research.arxiv import ARXIV_TOOLS
from inkwell.agent.tools.research.exa import EXA_TOOLS
from inkwell.agent.tools.research.fetch import FETCH_TOOLS
from inkwell.agent.tools.research.fred import FRED_TOOLS
from inkwell.agent.tools.research.markets import MARKET_TOOLS
from inkwell.agent.tools.research.wikipedia import WIKIPEDIA_TOOLS
from inkwell.agent.tools.stage_outputs import (
    AssumptionsCollector,
    AuthorNote,
    PlanCollector,
    ResearchCollector,
    ReviewCollector,
    make_assumptions_tools,
    make_note_tool,
    make_plan_tools,
    make_research_output_tools,
    make_review_output_tools,
)
from inkwell.agent.tools.voice import VoiceProfile, do_analyze_voice, load_style_corpus

logger = logging.getLogger(__name__)

BUILTIN_READ_TOOLS = ["Read", "Grep", "Glob"]
BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob"]


class PipelineError(Exception):
    """Raised when a pipeline stage fails to produce valid output."""


class TabEdit(BaseModel):
    """A detected author edit on a GDoc tab — diff only, not full content."""

    tab: str = Field(description="Tab label (e.g. 'Source', 'Section: Introduction')")
    diff: str = Field(description="Changed regions with surrounding context lines")
    original_snippet: str = Field(
        default="",
        description="Original text from the changed region, for revert suggestions",
    )


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


def extract_edit_summary(original: str, current: str, context_lines: int = 2) -> str:
    """Extract a concise summary of what changed between two texts."""
    orig_lines = original.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(None, orig_lines, curr_lines)

    blocks: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        ctx_before = orig_lines[max(0, i1 - context_lines):i1]
        ctx_after = orig_lines[i2:i2 + context_lines]

        parts: list[str] = []
        if ctx_before:
            parts.append("  " + "\n  ".join(ctx_before))

        match tag:
            case "replace":
                parts.append("- " + "\n- ".join(orig_lines[i1:i2]))
                parts.append("+ " + "\n+ ".join(curr_lines[j1:j2]))
            case "delete":
                parts.append("- " + "\n- ".join(orig_lines[i1:i2]))
            case "insert":
                parts.append("+ " + "\n+ ".join(curr_lines[j1:j2]))

        if ctx_after:
            parts.append("  " + "\n  ".join(ctx_after))

        blocks.append("\n".join(parts))

    return "\n---\n".join(blocks)


def extract_deleted_text(original: str, current: str) -> str:
    """Return the original text from deleted/replaced regions, for revert quoting."""
    orig_lines = original.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(None, orig_lines, curr_lines)
    deleted: list[str] = []
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            deleted.extend(orig_lines[i1:i2])
    return "\n".join(deleted)


# ---------------------------------------------------------------------------
# Pipeline listener — override for interactive behavior
# ---------------------------------------------------------------------------


class PipelineListener:
    """Receives pipeline events and collects author feedback."""

    async def on_stage(self, stage: str, description: str) -> None:
        logger.info("Pipeline: %s — %s", stage, description)

    async def on_progress(self, message: str) -> None:
        logger.info(message)

    async def on_complete(self, output: WritingOutput) -> None:
        logger.info("Pipeline complete: '%s'", output.title)

    async def on_message(self, source: str, message: str) -> None:
        logger.info("[%s] %s", source, message)

    async def collect_feedback(self, state: WritingSessionState) -> list[str]:
        comments = await state.get_new_author_comments()
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
        return None


# ---------------------------------------------------------------------------
# Tab edit detection
# ---------------------------------------------------------------------------


class TabTracker:
    """Tracks written tab content and detects author edits/suggestions."""

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.written: dict[str, tuple[str, str]] = {}

    def record(self, tab_id: str, label: str, content: str) -> None:
        self.written[tab_id] = (label, content)

    async def record_from_doc(self, tab_id: str, label: str) -> None:
        actual = await do_read_tab(self.doc_id, tab_id, as_markdown=True)
        self.written[tab_id] = (label, actual)

    async def detect_edits(self) -> list[TabEdit]:
        edits: list[TabEdit] = []
        for tab_id, (label, original) in self.written.items():
            try:
                current = await do_read_tab(
                    self.doc_id, tab_id, accept_suggestions=True, as_markdown=True
                )
            except (RuntimeError, OSError):
                continue
            if not self.has_meaningful_diff(original, current):
                continue
            diff = extract_edit_summary(original, current)
            original_snippet = extract_deleted_text(original, current)
            edits.append(TabEdit(tab=label, diff=diff, original_snippet=original_snippet))
            self.written[tab_id] = (label, current)
        return edits

    @staticmethod
    def has_meaningful_diff(original: str, current: str) -> bool:
        if not current.strip():
            return False
        return normalize_gdoc_text(original) != normalize_gdoc_text(current)


# ---------------------------------------------------------------------------
# MCP server builders
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


def build_output_server(
    server_name: str,
    tools: list[LupMcpTool],
) -> tuple[dict[str, McpServerConfig], list[str]]:
    """MCP server + allowed tool names for stage output tools."""
    server = create_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=extract_sdk_tools(tools),
    )
    tool_names = [f"mcp__{server_name}__{t.sdk_tool.name}" for t in tools]
    return {server_name: server}, tool_names


def build_note_server(
    stage: str,
    notes_collector: list[AuthorNote],
) -> tuple[dict[str, McpServerConfig], list[str]]:
    """Build an MCP server containing just the note_for_author tool."""
    note_tool = make_note_tool(notes_collector, stage)
    return build_output_server("notes", [note_tool])


# ---------------------------------------------------------------------------
# Pipeline stages — each reads from files, outputs via tools or Write
# ---------------------------------------------------------------------------


def annotate_draft_with_findings(
    draft: str, findings: list[ReviewFinding]
) -> str:
    """Inject review findings inline at matching text_excerpt locations."""
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


async def plan_article(
    notes: PipelineNotes,
    *,
    target_format: str = "lesswrong",
    voice_profile: VoiceProfile | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ArticlePlan:
    """Stage 1: Extract a structured article plan from source material."""
    conversation_path = notes.text_artifact_path("conversation")
    plan_path = notes.artifact_path("plan")
    collector = PlanCollector(plan_path)
    output_servers, output_tool_names = build_output_server("output", make_plan_tools(collector))
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("plan", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    voice_hint = ""
    corpus_samples, corpus_sources = load_style_corpus(max_samples=3)
    if corpus_samples:
        corpus_path = notes.artifacts_dir / "style_corpus.md"
        corpus_text = "\n\n---\n\n".join(s[:1500] for s in corpus_samples)
        corpus_path.write_text(corpus_text, encoding="utf-8")
        voice_hint += f"\nStyle corpus samples: {corpus_path}"
    if voice_profile:
        voice_path = notes.save_artifact("voice", voice_profile)
        voice_hint += f"\nVoice profile: {voice_path}"

    format_hint = (
        f"Suggested format: {target_format} (override if content is better "
        f"suited to another format: lesswrong, twitter, blog, dialog, memo)"
        if target_format
        else "Choose the best output format: lesswrong, twitter, blog, dialog, or memo"
    )
    task = (
        f"Extract a structured article plan from the source conversation.\n"
        f"{format_hint}\n\n"
        f"Source conversation file: {conversation_path}\n"
        f"{voice_hint}\n\n"
        f"Read the file, then build the plan using set_plan_header, "
        f"add_section, add_research_question, and add_source_quote."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=PLANNER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=output_servers,
        allowed_tools=output_tool_names,
        prefix="[plan] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    if not plan_path.exists():
        raise PipelineError("Planner produced no output (plan file not written)")
    return ArticlePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))


async def refine_plan(
    notes: PipelineNotes,
    *,
    voice_profile: VoiceProfile | None = None,
    feedback_path: Path | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ArticlePlan:
    """Refine the initial plan using research findings."""
    plan_path = notes.artifact_path("plan")
    research_path = notes.artifact_path("research")
    refined_path = notes.artifacts_dir / "plan_refined.json"
    collector = PlanCollector(refined_path)
    output_servers, output_tool_names = build_output_server("output", make_plan_tools(collector))
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("refine", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    file_refs = (
        f"Current plan: {plan_path}\n"
        f"Research findings: {research_path}\n"
    )
    if voice_profile:
        voice_path = notes.artifact_path("voice")
        file_refs += f"Voice profile: {voice_path}\n"
    if feedback_path:
        file_refs += f"Author feedback: {feedback_path}\n"

    task = (
        f"Refine the article plan using the research findings.\n\n"
        f"{file_refs}\n"
        f"Read both files, then build the refined plan using set_plan_header, "
        f"add_section, add_research_question, and add_source_quote."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=REFINER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=output_servers,
        allowed_tools=output_tool_names,
        prefix="[refine] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    if not refined_path.exists():
        return notes.load_artifact("plan", ArticlePlan) or ArticlePlan(
            title="Untitled", thesis="", target_format="lesswrong",
            sections=[], research_questions=[], source_quotes=[],
            author_direction="", voice_notes="",
        )
    refined = ArticlePlan.model_validate_json(refined_path.read_text(encoding="utf-8"))
    notes.save_artifact("plan", refined)
    return refined


async def research_plan(
    notes: PipelineNotes,
    *,
    feedback_path: Path | None = None,
    servers: dict[str, McpServerConfig] | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ResearchCompilation:
    """Stage 2: Deep research on all questions from the article plan."""
    if servers is None:
        servers = build_research_servers()

    plan_path = notes.artifact_path("plan")
    research_path = notes.artifact_path("research")
    collector = ResearchCollector(research_path)
    output_servers, output_tool_names = build_output_server(
        "output", make_research_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("research", note_collector)
    all_servers = {**servers, **output_servers, **note_servers}
    all_tools = research_tool_names() + output_tool_names + note_tool_names

    file_refs = f"Article plan: {plan_path}\n"
    if feedback_path:
        file_refs += f"Author feedback: {feedback_path}\n"

    task = (
        f"Research all questions from the article plan.\n\n"
        f"{file_refs}\n"
        f"Read the plan to find the research questions, then investigate "
        f"each one. After researching, call record_finding for each answer."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=RESEARCHER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[research] ",
        cost_accumulator=cost_accumulator,
    )
    if not research_path.exists():
        raise PipelineError("Researcher produced no output (research file not written)")
    return ResearchCompilation.model_validate_json(
        research_path.read_text(encoding="utf-8")
    )


async def write_section(
    section_title: str,
    *,
    notes: PipelineNotes,
    draft_path: Path,
    voice_profile: VoiceProfile | None = None,
    feedback_path: Path | None = None,
    section_context: str = "",
    servers: dict[str, McpServerConfig] | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> SectionDraft:
    """Write a single section using built-in Write. Called in parallel."""
    if servers is None:
        servers = build_research_servers()

    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server(f"write:{section_title}", note_collector)
    all_servers = {**servers, **note_servers}
    all_tools = research_tool_names() + note_tool_names

    plan_path = notes.artifact_path("plan")
    research_path = notes.artifact_path("research")

    file_refs = (
        f"Article plan: {plan_path}\n"
        f"Research findings: {research_path}\n"
    )
    if voice_profile:
        voice_path = notes.artifact_path("voice")
        file_refs += f"Voice profile: {voice_path}\n"
    if feedback_path:
        file_refs += f"Author feedback: {feedback_path}\n"

    task = (
        f'Write the section "{section_title}" for the article.\n\n'
        f"{file_refs}\n"
    )
    if section_context:
        task += f"{section_context}\n\n"
    task += (
        f"Read the plan to find this section's details (summary, key points, "
        f"quotes to include), then read the research findings for relevant "
        f"context. If a voice profile is available, read it to match the "
        f"author's style.\n\n"
        f"Write the complete section to: {draft_path}\n\n"
        f"Use the Write tool to write the file. Write the entire section "
        f"in one call. If you have questions for the author, call note_for_author."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=SECTION_WRITER_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix=f"[write:{section_title}] ",
        cost_accumulator=cost_accumulator,
    )

    if draft_path.exists():
        content = draft_path.read_text(encoding="utf-8")
    else:
        raise PipelineError(
            f"Section writer for '{section_title}' produced no output"
        )

    return SectionDraft(
        title=section_title,
        content=content,
        word_count=len(content.split()),
        questions_for_author=note_collector,
    )


async def plan_merge(
    section_draft_paths: dict[str, Path],
    *,
    notes: PipelineNotes,
    output_path: Path,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> Path:
    """Phase 1 of merge: produce a structural plan (deduplication, transitions, cuts)."""
    drafts_list = "\n".join(
        f"- {title}: {path}" for title, path in section_draft_paths.items()
    )

    plan_path = notes.artifact_path("plan")
    task = (
        f"Analyze these independently-written sections and produce a merge plan.\n\n"
        f"Section draft files:\n{drafts_list}\n"
        f"Article plan: {plan_path}\n\n"
        f"Read each section draft and the article plan. Identify duplication, "
        f"missing transitions, redundant openings, and structural issues.\n\n"
        f"Write the merge plan to: {output_path}"
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=MERGE_PLAN_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        trace_logger=trace_logger,
        prefix="[merge:plan] ",
        cost_accumulator=cost_accumulator,
    )

    if not output_path.exists():
        raise PipelineError("Merge planner produced no output")
    return output_path


async def merge_sections(
    section_draft_paths: dict[str, Path],
    *,
    notes: PipelineNotes,
    output_path: Path,
    voice_profile: VoiceProfile | None = None,
    feedback_path: Path | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    heartbeat: HeartbeatCallback | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> MergedDraft:
    """Stage 4: Two-phase merge of independently-written sections.

    Phase 1: Structural plan — identify duplication, plan transitions, decide cuts.
    Phase 2: Rewrite — execute the plan as unified prose.
    """
    merge_plan_path = notes.artifacts_dir / "merge_plan.md"
    await plan_merge(
        section_draft_paths,
        notes=notes,
        output_path=merge_plan_path,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("merge", note_collector)

    drafts_list = "\n".join(
        f"- {title}: {path}" for title, path in section_draft_paths.items()
    )

    file_refs = (
        f"Merge plan (read this FIRST): {merge_plan_path}\n"
        f"Section draft files:\n{drafts_list}\n"
    )
    if voice_profile:
        voice_path = notes.artifact_path("voice")
        file_refs += f"Voice profile: {voice_path}\n"
    if feedback_path:
        file_refs += f"Author feedback: {feedback_path}\n"

    plan_path = notes.artifact_path("plan")
    file_refs += f"Article plan: {plan_path}\n"

    task = (
        f"Rewrite these sections into a unified article using the merge plan.\n\n"
        f"{file_refs}\n"
        f"Read the merge plan FIRST — it tells you what to cut, how to "
        f"connect sections, and where duplication exists. Then read the "
        f"section drafts as raw material.\n\n"
        f"Write the complete merged draft to: {output_path}"
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=COHERENCE_EDITOR_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=note_servers,
        allowed_tools=note_tool_names,
        trace_logger=trace_logger,
        prefix="[merge:rewrite] ",
        heartbeat=heartbeat,
        cost_accumulator=cost_accumulator,
    )

    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
    else:
        raise PipelineError("Coherence editor produced no output")

    return MergedDraft(content=content, changes_made=[])


async def review_narrative(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for narrative coherence (tool-free except output)."""
    plan_path = notes.artifact_path("plan")
    review_path = notes.artifacts_dir / "review_narrative.json"
    collector = ReviewCollector(review_path, "narrative")
    output_servers, output_tool_names = build_output_server(
        "output", make_review_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("review:narrative", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    task = (
        f"Review the article draft for narrative coherence.\n\n"
        f"Draft: {draft_path}\n"
        f"Plan: {plan_path}\n\n"
        f"Read both files, then call record_finding for each issue."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=NARRATIVE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=output_servers,
        allowed_tools=output_tool_names,
        trace_logger=trace_logger,
        prefix="[review:narrative] ",
        cost_accumulator=cost_accumulator,
    )
    if review_path.exists():
        data = json.loads(review_path.read_text(encoding="utf-8"))
        findings = [ReviewFinding.model_validate(f) for f in data.get("findings", [])]
    else:
        findings = []
    return ReviewOutput(findings=findings)


async def review_facts(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    servers: dict[str, McpServerConfig] | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for factual accuracy (needs research tools to verify)."""
    if servers is None:
        servers = build_research_servers()

    review_path = notes.artifacts_dir / "review_factcheck.json"
    collector = ReviewCollector(review_path, "factcheck")
    output_servers, output_tool_names = build_output_server(
        "output", make_review_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("review:facts", note_collector)
    all_servers = {**servers, **output_servers, **note_servers}
    all_tools = review_tool_names() + output_tool_names + note_tool_names

    task = (
        f"Fact-check every verifiable claim in the article draft.\n\n"
        f"Draft: {draft_path}\n\n"
        f"Read the file, then verify claims using your research tools. "
        f"Call record_finding for each issue."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=FACT_CHECKER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:facts] ",
        cost_accumulator=cost_accumulator,
    )
    if review_path.exists():
        data = json.loads(review_path.read_text(encoding="utf-8"))
        findings = [ReviewFinding.model_validate(f) for f in data.get("findings", [])]
    else:
        findings = []
    return ReviewOutput(findings=findings)


async def review_style(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for writing quality and voice consistency (tool-free except output)."""
    voice_path = notes.artifact_path("voice")
    review_path = notes.artifacts_dir / "review_style.json"
    collector = ReviewCollector(review_path, "style")
    output_servers, output_tool_names = build_output_server(
        "output", make_review_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("review:style", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    file_refs = f"Draft: {draft_path}\n"
    if voice_path.exists():
        file_refs += f"Voice profile: {voice_path}\n"

    task = (
        f"Review the article draft for writing quality and style.\n\n"
        f"{file_refs}\n"
        f"Read the files, then call record_finding for each issue."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=STYLE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=output_servers,
        allowed_tools=output_tool_names,
        trace_logger=trace_logger,
        prefix="[review:style] ",
        cost_accumulator=cost_accumulator,
    )
    if review_path.exists():
        data = json.loads(review_path.read_text(encoding="utf-8"))
        findings = [ReviewFinding.model_validate(f) for f in data.get("findings", [])]
    else:
        findings = []
    return ReviewOutput(findings=findings)


async def review_all(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    servers: dict[str, McpServerConfig] | None = None,
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[ReviewFinding]:
    """Stage 5: Run all three reviewers in parallel."""
    narrative_task = review_narrative(
        notes, draft_path,
        author_notes=author_notes,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    facts_task = review_facts(
        notes, draft_path,
        servers=servers,
        author_notes=author_notes,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    style_task = review_style(
        notes, draft_path,
        author_notes=author_notes,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    results = await asyncio.gather(
        narrative_task, facts_task, style_task,
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
    notes: PipelineNotes,
    draft_path: Path,
    findings: list[ReviewFinding],
    *,
    output_path: Path,
    voice_profile: VoiceProfile | None = None,
    feedback_path: Path | None = None,
    target_format: str = "lesswrong",
    author_notes: list[AuthorNote] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Stage 6: Incorporate all feedback and produce the final article."""
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("rewrite", note_collector)
    plan_path = notes.artifact_path("plan")

    # Write annotated draft with inline findings to a temp file
    draft_content = draft_path.read_text(encoding="utf-8")
    annotated = annotate_draft_with_findings(draft_content, findings)
    annotated_path = notes.artifacts_dir / "draft_annotated.md"
    annotated_path.write_text(annotated, encoding="utf-8")

    # Write review summary
    review_summary_path = notes.artifacts_dir / "review_summary.md"
    n_critical = sum(1 for f in findings if f.severity == "critical")
    n_suggestion = sum(1 for f in findings if f.severity == "suggestion")
    review_summary_path.write_text(
        f"# Review Summary\n\n"
        f"**{n_critical} critical** (mandatory), **{n_suggestion} suggestions**\n\n"
        f"Review findings are annotated inline in the draft file.\n",
        encoding="utf-8",
    )

    file_refs = (
        f"Annotated draft (with inline findings): {annotated_path}\n"
        f"Article plan: {plan_path}\n"
        f"Review summary: {review_summary_path}\n"
    )
    if voice_profile:
        voice_path = notes.artifact_path("voice")
        file_refs += f"Voice profile: {voice_path}\n"
    if feedback_path:
        file_refs += f"Author feedback: {feedback_path}\n"

    task = (
        f"Produce the final version of the article.\n\n"
        f"Target format: {target_format}\n\n"
        f"{file_refs}\n"
        f"Read the annotated draft — review findings are marked inline. "
        f"Apply all critical findings and worthwhile suggestions.\n\n"
        f"Write the final article to: {output_path}"
    )

    collector = await query(
        task,
        model="claude-opus-4-6",
        system_prompt=REWRITER_SYSTEM,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=note_servers,
        allowed_tools=note_tool_names,
        trace_logger=trace_logger,
        prefix="[rewrite] ",
        cost_accumulator=cost_accumulator,
    )

    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
    else:
        raise PipelineError("Rewriter produced no output")

    plan = notes.load_artifact("plan", ArticlePlan)
    title = plan.title if plan else "Untitled"
    summary = collector.text.strip() if collector.text else ""

    return WritingOutput(
        title=title,
        content=content,
        word_count=len(content.split()),
        summary=summary,
        review_findings=findings,
    )


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
# Assumptions + orchestrator (assumptions uses output tools, orchestrator
# keeps structured output)
# ---------------------------------------------------------------------------


async def surface_assumptions(
    notes: PipelineNotes,
    doc_id: str,
    *,
    has_research: bool = False,
    session_state: WritingSessionState,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> AssumptionsList:
    """Surface uncertainties and questions as GDoc comments."""
    plan_path = notes.artifact_path("plan")
    assumptions_path = notes.artifact_path("assumptions")
    collector = AssumptionsCollector(assumptions_path)
    output_servers, output_tool_names = build_output_server(
        "output", make_assumptions_tools(collector)
    )

    file_refs = f"Article plan: {plan_path}\n"
    if has_research:
        research_path = notes.artifact_path("research")
        file_refs += f"Research findings: {research_path}\n"

    task = (
        f"Review the article plan and surface all uncertainties.\n\n"
        f"{file_refs}\n"
        f"Read the files, then call record_assumption for each uncertainty."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=ASSUMPTIONS_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=None,
        permission_mode="bypassPermissions",
        mcp_servers=output_servers,
        allowed_tools=output_tool_names,
        prefix="[assumptions] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    if assumptions_path.exists():
        result = AssumptionsList.model_validate_json(
            assumptions_path.read_text(encoding="utf-8")
        )
    else:
        result = AssumptionsList(items=[])

    tag_prefix = {
        "direction_check": "[DIRECTION]",
        "assumption": "[ASSUMPTION]",
        "question": "[QUESTION]",
        "confusion": "[UNCLEAR]",
    }
    for item in result.items:
        async with gdoc_nonfatal("post assumption comment"):
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
    """Run the orchestrator to produce a restart strategy. Keeps structured output."""
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
        max_thinking_tokens=None,
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
    """State-machine wrapper around the pipeline stages."""

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

        self.author_notes: list[AuthorNote] = []
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

    def ensure_notes(self) -> PipelineNotes:
        """Return notes, creating a temp dir if needed."""
        if self.notes is None:
            self.notes = PipelineNotes(Path(tempfile.mkdtemp(prefix="inkwell-notes-")))
        return self.notes

    def get_draft_path(self, label: str) -> Path:
        slug = label.lower().replace(" ", "-")
        for ch in "/:.,;!?\"'()[]{}":
            slug = slug.replace(ch, "")
        while "--" in slug:
            slug = slug.replace("--", "-")
        slug = slug.strip("-")
        return self.ensure_notes().drafts_dir / f"{slug}.md"

    async def save_snapshot(self) -> None:
        self.snapshot.doc_id = self.state.doc_id
        self.snapshot.doc_url = self.state.doc_url
        self.snapshot.seen_comment_ids = set(self.state.seen_comment_ids)
        self.snapshot.agent_comment_ids = set(self.state.agent_comment_ids)
        self.snapshot.pending_questions = list(self.state.pending_questions)

        notes = self.ensure_notes()
        data = self.snapshot.model_dump_json()
        base = notes.base_dir
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

        if snapshot.doc_id:
            self.existing_doc_id = snapshot.doc_id
        elif snapshot.output and snapshot.output.google_doc_id:
            self.existing_doc_id = snapshot.output.google_doc_id

        self.state.seen_comment_ids = set(snapshot.seen_comment_ids)
        self.state.agent_comment_ids = set(snapshot.agent_comment_ids)
        self.state.pending_questions = list(snapshot.pending_questions)

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
        if self.watcher is not None:
            await self.watcher.stop()
            self.watcher = None
            logger.info("Comment watcher stopped")

    async def setup_doc(self) -> None:
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

        async with gdoc_nonfatal("write initial overview"):
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
        assert self.tabs is not None
        comment_feedback = await self.hooks.collect_feedback(self.state)
        tab_edits = await self.tabs.detect_edits()

        notes = self.ensure_notes()
        for fb in comment_feedback:
            await notes.add_terminal_input(fb)
        for edit in tab_edits:
            await self.classify_and_record_edit(edit)

        return comment_feedback

    async def post_author_notes(self) -> None:
        """Post any new author notes as GDoc comments, then clear the list."""
        while self.author_notes:
            note = self.author_notes.pop(0)
            tag = f"[{note.stage.upper()}]" if note.stage else "[NOTE]"
            async with gdoc_nonfatal("post author note"):
                await do_insert_comment(
                    self.doc_id,
                    f"{tag} {note.note}",
                    anchor_text=note.anchor or None,
                    session_state=self.state,
                )
                self.state.add_question(note.note)

    async def classify_and_record_edit(self, edit: TabEdit) -> None:
        task = (
            f"Classify this author edit on the '{edit.tab}' tab:\n\n"
            f"Changes:\n{edit.diff}"
        )
        classified = await query(
            task,
            output_type=ClassifiedComment,
            model="claude-opus-4-6",
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=None,
            permission_mode="bypassPermissions",
            prefix=f"[classify-edit:{edit.tab}] ",
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        if classified is None:
            logger.warning("Failed to classify edit on tab '%s'", edit.tab)
            return

        if classified.impact == "dismiss":
            logger.debug("Dismissed noise edit on '%s'", edit.tab)
            return

        if classified.impact == "revert_suggested":
            logger.info("Suggesting revert for accidental edit on '%s'", edit.tab)
            async with gdoc_nonfatal("post revert suggestion"):
                snippet = edit.original_snippet[:500] if edit.original_snippet else edit.diff
                await do_insert_comment(
                    self.doc_id,
                    (
                        f"[REVERT CHECK] This edit on '{edit.tab}' looks like it "
                        f"might be accidental. Did you mean to make this change?\n\n"
                        f"Original text:\n{snippet}\n\n"
                        f"(Reply 'yes' to keep the edit, or 'no' / ignore to revert.)"
                    ),
                    anchor_text=edit.original_snippet[:200] if edit.original_snippet else None,
                    session_state=self.state,
                )
            return

        classified.comment_id = f"tab-edit-{edit.tab}"
        classified.content = f"[Edit on {edit.tab}] {edit.diff}"
        classified.anchor_text = edit.tab

        notes = self.ensure_notes()
        await notes.add_comment(classified)

        if classified.impact == "plan_breaking":
            logger.info("Plan-breaking tab edit on '%s'", edit.tab)
            self.plan_breaking.set()

    async def prepare_feedback(self, stage: str) -> Path | None:
        """Gather feedback and render to a file. Returns path or None."""
        feedback = await self.gather_feedback()
        notes = self.ensure_notes()
        feedback_with_notes = await self.format_stage_context(stage, feedback)
        if not feedback_with_notes:
            return None
        return await notes.render_feedback_file(stage, feedback_with_notes)

    async def stage_extract(self) -> None:
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
        notes = self.ensure_notes()
        notes.save_text_artifact("conversation", conversation)
        await self.save_snapshot()

        async with gdoc_nonfatal("write source tab"):
            source_tab_id = self.known_tabs["Source"]
            await do_write_tab(
                self.doc_id, source_tab_id, conversation, session_state=self.state
            )
            assert self.tabs is not None
            await self.tabs.record_from_doc(source_tab_id, "Source")
        await self.update_overview()

    async def stage_voice(self) -> None:
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

        notes = self.ensure_notes()
        notes.save_artifact("voice", voice_profile)
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
            async with gdoc_nonfatal("write voice tab"):
                voice_tab_id = self.known_tabs["Voice"]
                await do_write_tab(
                    self.doc_id, voice_tab_id, voice_text, session_state=self.state
                )
                assert self.tabs is not None
                await self.tabs.record_from_doc(voice_tab_id, "Voice")
        await self.update_overview()

    async def stage_plan(self) -> None:
        await self.hooks.on_stage("plan", "Planning article structure")
        await self.update_overview(active_stage="plan")
        notes = self.ensure_notes()

        plan = await plan_article(
            notes,
            target_format=self.target_format,
            voice_profile=self.snapshot.voice_profile,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
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
        plan = self.snapshot.plan
        if plan is None:
            return
        await self.hooks.on_stage("assumptions", "Surfacing questions for the author")
        await self.update_overview(active_stage="assumptions")
        notes = self.ensure_notes()

        assumptions = await surface_assumptions(
            notes,
            self.doc_id,
            has_research=self.snapshot.research is not None,
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
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot research without a plan")

        await self.hooks.on_stage(
            "research",
            f"Researching {len(plan.research_questions)} questions",
        )
        self.state.set_stage("researching")
        await self.update_overview(active_stage="research")
        feedback_path = await self.prepare_feedback("research")
        notes = self.ensure_notes()

        research = await research_plan(
            notes,
            feedback_path=feedback_path,
            servers=self.research_servers,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
        self.snapshot.research = research
        self.snapshot.stage = "research"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Research complete: {len(research.findings)} findings"
        )
        await self.write_research_tab(research)
        await self.update_overview()

    async def stage_refine(self) -> None:
        plan = self.snapshot.plan
        research = self.snapshot.research
        if plan is None or research is None:
            return

        await self.hooks.on_stage(
            "refine",
            f"Refining plan with {len(research.findings)} research findings",
        )
        await self.update_overview(active_stage="refine")
        feedback_path = await self.prepare_feedback("refine")
        notes = self.ensure_notes()
        initial_count = len(plan.sections)

        refined = await refine_plan(
            notes,
            voice_profile=self.snapshot.voice_profile,
            feedback_path=feedback_path,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
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

        feedback_path = await self.prepare_feedback("write")
        notes = self.ensure_notes()

        def build_neighbor_context(idx: int) -> str:
            assert plan is not None
            parts: list[str] = []
            if idx > 0:
                prev = plan.sections[idx - 1]
                parts.append(f'Previous section: "{prev.title}" — {prev.summary}')
            if idx < len(plan.sections) - 1:
                nxt = plan.sections[idx + 1]
                parts.append(f'Next section: "{nxt.title}" — {nxt.summary}')
            if not parts:
                return ""
            return (
                "## Adjacent Sections\n\n"
                + "\n".join(parts)
                + "\n\nConnect to adjacent sections — don't re-establish "
                "context the reader will already have."
            )

        async def write_and_publish(section: SectionPlan, idx: int) -> SectionDraft:
            draft = await write_section(
                section.title,
                notes=notes,
                draft_path=self.get_draft_path(section.title),
                voice_profile=self.snapshot.voice_profile,
                feedback_path=feedback_path,
                section_context=build_neighbor_context(idx),
                servers=self.research_servers,
                author_notes=self.author_notes,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
            self.snapshot.section_drafts[section.title] = draft

            async with gdoc_nonfatal(f"write section '{section.title}'"):
                assert self.tabs is not None
                tid = self.tab_ids.get(section.title, "")
                if tid and draft.content and not draft.content.startswith("[Section failed"):
                    await do_write_tab(
                        self.doc_id, tid, draft.content, session_state=self.state
                    )
                    await self.tabs.record_from_doc(tid, f"Section: {section.title}")
                    self.state.update_section_status(section.title, "drafted")

            await self.hooks.on_message(
                "write",
                f"Section '{draft.title}' complete ({draft.word_count} words)",
            )
            await self.update_overview(active_stage="write")
            return draft

        results = await asyncio.gather(
            *(write_and_publish(s, i) for i, s in enumerate(plan.sections)),
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

        await self.post_author_notes()
        self.snapshot.stage = "write"
        await self.save_snapshot()
        await self.update_overview()

    async def stage_merge(self) -> None:
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot merge without a plan")

        await self.hooks.on_stage("merge", "Merging sections into coherent draft")
        self.state.set_stage("merging")
        await self.update_overview(active_stage="merge")
        feedback_path = await self.prepare_feedback("merge")
        notes = self.ensure_notes()

        async def merge_heartbeat(elapsed: float) -> None:
            mins, secs = divmod(int(elapsed), 60)
            await self.hooks.on_progress(f"Merging... ({mins}m{secs:02d}s elapsed)")

        section_paths = {
            title: self.get_draft_path(title)
            for title, draft in self.snapshot.section_drafts.items()
            if not draft.content.startswith("[Section failed")
        }
        output_path = self.get_draft_path("merged")

        merged = await merge_sections(
            section_paths,
            notes=notes,
            output_path=output_path,
            voice_profile=self.snapshot.voice_profile,
            feedback_path=feedback_path,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            heartbeat=merge_heartbeat,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
        self.snapshot.merged = merged
        self.snapshot.stage = "merge"
        await self.save_snapshot()

        async with gdoc_nonfatal("write draft tab"):
            if "Draft" not in self.known_tabs:
                self.known_tabs["Draft"] = await do_create_tab(
                    self.doc_id, "Draft", session_state=self.state
                )
            self.draft_tab_id = self.known_tabs["Draft"]
            await do_write_tab(
                self.doc_id, self.draft_tab_id, merged.content, session_state=self.state
            )
            assert self.tabs is not None
            await self.tabs.record_from_doc(self.draft_tab_id, "Draft")
        await self.update_overview()

    async def stage_review(self) -> None:
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot review without plan and merged draft")

        await self.hooks.on_stage("review", "Reviewing draft (3 reviewers in parallel)")
        self.state.set_stage("reviewing")
        await self.update_overview(active_stage="review")

        notes = self.ensure_notes()
        draft_path = self.get_draft_path("merged")

        findings = await review_all(
            notes, draft_path,
            servers=self.research_servers,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
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
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot rewrite without plan and merged draft")

        await self.hooks.on_stage("rewrite", "Producing final version")
        self.state.set_stage("rewriting")
        await self.update_overview(active_stage="rewrite")
        feedback_path = await self.prepare_feedback("rewrite")
        notes = self.ensure_notes()

        draft_path = self.get_draft_path("merged")
        output_path = self.get_draft_path("final")

        output = await rewrite_final(
            notes,
            draft_path,
            self.snapshot.findings,
            output_path=output_path,
            voice_profile=self.snapshot.voice_profile,
            feedback_path=feedback_path,
            target_format=self.target_format,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
        output.voice_profile = self.snapshot.voice_profile
        output.open_questions = list(self.state.pending_questions)
        self.snapshot.output = output
        self.snapshot.stage = "rewrite"
        await self.save_snapshot()
        await self.update_overview()

    async def stage_format(self) -> None:
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

        async with gdoc_nonfatal("write final tab"):
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
        has_signal = self.plan_breaking.is_set()
        notes = self.ensure_notes()
        has_notes = await notes.has_plan_breaking()

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
        notes = self.ensure_notes()
        if strategy.new_plan:
            self.snapshot.plan = strategy.new_plan
            notes.save_artifact("plan", strategy.new_plan)
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
        draft = self.snapshot.section_drafts.get(section)
        if draft is None:
            logger.warning("Cannot patch missing section: %s", section)
            return

        patch_path = self.get_draft_path(section)
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(draft.content, encoding="utf-8")

        task = (
            f"Apply this targeted edit to the section.\n\n"
            f"## Section: {section}\n\n"
            f"Draft file: {patch_path}\n\n"
            f"## Edit Target\n\n\"{target_text}\"\n\n"
            f"## Instruction\n\n{instruction}\n\n"
            f"Read the draft with the Read tool, then use Edit to apply "
            f"the change. Use Write if the change is too large for Edit."
        )
        await query(
            task,
            model="claude-opus-4-6",
            system_prompt=SECTION_WRITER_PROMPT,
            tools=BUILTIN_WRITE_TOOLS,
            max_thinking_tokens=None,
            permission_mode="bypassPermissions",
            prefix=f"[patch:{section}] ",
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )

        if patch_path.exists():
            content = patch_path.read_text(encoding="utf-8")
            self.snapshot.section_drafts[section] = SectionDraft(
                title=section,
                content=content,
                word_count=len(content.split()),
            )

    async def execute_rewrites(
        self,
        rewrite_tasks: list[tuple[SectionPlan, list[str]]],
        add_tasks: list[SectionPlan],
    ) -> None:
        plan = self.snapshot.plan
        if plan is None:
            return

        notes = self.ensure_notes()
        all_plans: list[SectionPlan] = []
        all_coros = []
        for section_plan, _questions in rewrite_tasks:
            all_plans.append(section_plan)
            all_coros.append(
                write_section(
                    section_plan.title,
                    notes=notes,
                    draft_path=self.get_draft_path(section_plan.title),
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    author_notes=self.author_notes,
                    trace_logger=self.trace_logger,
                    cost_accumulator=self.cost_accumulator,
                )
            )
        for section_plan in add_tasks:
            all_plans.append(section_plan)
            all_coros.append(
                write_section(
                    section_plan.title,
                    notes=notes,
                    draft_path=self.get_draft_path(section_plan.title),
                    voice_profile=self.snapshot.voice_profile,
                    servers=self.research_servers,
                    author_notes=self.author_notes,
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
        plan = self.snapshot.plan
        if plan is None:
            return None
        for s in plan.sections:
            if s.title == section_title:
                return s
        return None

    # -- Standby loop ------------------------------------------

    async def standby_loop(
        self, initial_wait: float = 120.0, quiet_wait: float = 90.0
    ) -> None:
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
            assert self.tabs is not None
            for edit in await self.tabs.detect_edits():
                await self.classify_and_record_edit(edit)
            all_feedback = await self.format_stage_context("revise", all_feedback)

            plan = self.snapshot.plan
            if plan is None:
                break

            notes = self.ensure_notes()
            has_plan_breaking = await notes.has_plan_breaking()
            if has_plan_breaking and self.restart_count < self.max_restarts:
                await self.check_and_maybe_restart()
                await notes.clear_plan_breaking()

            await self.do_standby_rewrite(all_feedback)

    async def do_standby_rewrite(self, feedback: list[str]) -> None:
        plan = self.snapshot.plan
        if plan is None:
            return

        notes = self.ensure_notes()

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

        # Write current content to a temp draft for the rewriter to read
        standby_draft = self.get_draft_path("standby-input")
        standby_draft.write_text(current_content, encoding="utf-8")
        standby_output = self.get_draft_path("final-standby")

        feedback_path = await notes.render_feedback_file("standby", feedback)

        output = await rewrite_final(
            notes,
            standby_draft,
            self.snapshot.findings,
            output_path=standby_output,
            voice_profile=self.snapshot.voice_profile,
            feedback_path=feedback_path,
            target_format=self.target_format,
            author_notes=self.author_notes,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()

        article_text = output.content or output.summary
        final_content = await apply_format(
            article_text, plan.title, self.target_format
        )
        async with gdoc_nonfatal("write standby rewrite"):
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
        notes = self.ensure_notes()

        match stage:
            case "research":
                comments = await notes.list_comments(impact="plan_breaking")
                comments.extend(await notes.list_comments(impact="stage_local"))
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
                    section_fb = await notes.get_section_feedback(section)
                    if section_fb:
                        feedback = [*feedback, section_fb]
                else:
                    notes_text = await notes.get_all_feedback()
                    if notes_text:
                        feedback = [*feedback, notes_text]

            case "merge" | "review" | "rewrite" | "revise":
                notes_text = await notes.get_all_feedback()
                if notes_text:
                    feedback = [*feedback, notes_text]

            case _:
                notes_text = await notes.get_all_feedback()
                if notes_text:
                    feedback = [*feedback, notes_text]

        return feedback

    # -- Tab helpers ------------------------------------------------------

    async def write_plan_tab(self, plan: ArticlePlan) -> None:
        async with gdoc_nonfatal("write plan tab"):
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
            await self.tabs.record_from_doc(plan_tab_id, "Plan")

    async def create_section_tabs(self, plan: ArticlePlan) -> None:
        async with gdoc_nonfatal("rename doc"):
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
            tab_name = truncate_tab_title(f"§{i + 1} {section.title}")
            if tab_name in self.known_tabs:
                self.tab_ids[section.title] = self.known_tabs[tab_name]
                self.state.add_section(tab_name, self.known_tabs[tab_name])
            else:
                async with gdoc_nonfatal(f"create tab '{tab_name}'"):
                    tid = await do_create_tab(
                        self.doc_id, tab_name,
                        parent_tab_id=sections_parent_id,
                        session_state=self.state,
                    )
                    self.tab_ids[section.title] = tid
                    self.known_tabs[tab_name] = tid

        async with gdoc_nonfatal("write sections index"):
            sections_summary = "\n".join(
                f"- §{i + 1} {s.title}" for i, s in enumerate(plan.sections)
            )
            await do_write_tab(
                self.doc_id, sections_parent_id,
                f"# Sections ({len(plan.sections)})\n\n{sections_summary}",
                session_state=self.state,
            )

    async def write_research_tab(self, research: ResearchCompilation) -> None:
        async with gdoc_nonfatal("write research tab"):
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
            await self.tabs.record_from_doc(research_tab_id, "Research")

    async def write_review_tab(self, findings: list[ReviewFinding]) -> None:
        async with gdoc_nonfatal("write review tab"):
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
                    async with gdoc_nonfatal("post review comment"):
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
        async with gdoc_nonfatal("update overview"):
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
    """Run the complete writing pipeline."""
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
