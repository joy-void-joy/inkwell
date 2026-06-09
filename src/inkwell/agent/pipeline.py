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
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NamedTuple


from claude_agent_sdk import McpServerConfig
from pydantic import BaseModel, Field

from lup.client import CostAccumulator, HeartbeatCallback, active_block_callback, query
from lup.mcp import LupMcpTool, create_mcp_server, extract_sdk_tools
from lup.sandbox import Sandbox
from lup.trace import TraceLogger, extract_block_info

import inkwell.agent.config as config_mod
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

from inkwell.agent.content import ContentManifest
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.watcher import (
    ACKNOWLEDGE_TEMPLATES,
    create_comment_watcher,
    create_source_watcher,
)
from inkwell.agent.session import AuthorComment, WritingSessionState
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
    get_format_guidance,
)
from inkwell.agent.stages import EXTRACTOR_PROMPT
from inkwell.agent.tool_policy import research_tool_names, review_tool_names
from inkwell.agent.tools.extract import (
    EXTRACT_TOOLS as EXTRACT_MCP_TOOLS,
    GDOC_URL_PATTERN,
    do_extract_conversation,
    do_extract_file,
    do_extract_gdoc,
    do_extract_lesswrong,
    extract_source_tab_only,
    fetch_gdoc_comments,
    parse_gdoc_id,
)
from inkwell.agent.tools.research.fetch import do_fetch_source
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
    CommentSpec,
    configure_session_state,
    do_create_doc,
    do_create_tab,
    do_insert_comment,
    do_insert_comments_batch,
    do_list_tabs,
    do_read_tab,
    do_rename_doc,
    do_reply_to_comment,
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
from inkwell.agent.tools.query_artifacts import make_query_tools
from inkwell.agent.tools.voice import (
    analyze_voice_individually,
    compute_voice_fingerprint,
    invalidate_merged_cache,
    load_style_corpus,
)

logger = logging.getLogger(__name__)

BUILTIN_READ_TOOLS = ["Read", "Grep", "Glob", "Agent"]
BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Agent"]


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
    ("‘", "'"),
    ("’", "'"),
    ("“", '"'),
    ("”", '"'),
    ("—", "--"),
    ("–", "-"),
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

        ctx_before = orig_lines[max(0, i1 - context_lines) : i1]
        ctx_after = orig_lines[i2 : i2 + context_lines]

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
# Slugify + research splitting
# ---------------------------------------------------------------------------


def slugify(label: str) -> str:
    """Turn a section title or label into a filesystem-safe slug."""
    slug = label.lower().replace(" ", "-")
    for ch in "/:.,;!?\"'()[]{}":
        slug = slug.replace(ch, "")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:80]


def add_voice_refs(manifest: ContentManifest, voice_file_paths: list[str]) -> None:
    """Add voice/style file references to a content manifest."""
    for p in voice_file_paths:
        name = Path(p).stem
        if name.startswith("prescriptive_"):
            manifest.add(
                p,
                "prescriptive_rules",
                name,
                instruction="apply verbatim as hard constraints",
            )
        elif name.startswith("corpus_"):
            manifest.add(
                p, "style_reference", name, instruction="match tone, not content"
            )
        elif name.startswith("voice_"):
            manifest.add(p, "voice_analysis", name, instruction="match this voice")
        else:
            manifest.add(p, "voice_analysis", name)


# ---------------------------------------------------------------------------
# Pipeline listener — override for interactive behavior
# ---------------------------------------------------------------------------


class PipelineListener:
    """Receives pipeline events and collects author input from the environment.

    GDoc comments and tab edits are ingested by the pipeline itself
    (classified, recorded, acknowledged) — listeners only supply input
    typed in their environment (terminal, web UI).
    """

    def __init__(self) -> None:
        self.sync_requested = asyncio.Event()

    def request_sync(self) -> None:
        self.sync_requested.set()

    async def on_stage(self, stage: str, description: str) -> None:
        logger.info("Pipeline: %s — %s", stage, description)

    async def on_progress(self, message: str) -> None:
        logger.info(message)

    async def on_complete(self, output: WritingOutput) -> None:
        logger.info("Pipeline complete: '%s'", output.title)

    async def on_block(self, _block_type: str, _content: str, _prefix: str) -> None:
        pass

    async def on_message(self, source: str, message: str) -> None:
        logger.info("[%s] %s", source, message)

    async def collect_author_input(self, state: WritingSessionState) -> list[str]:
        """Return new author directions typed in the environment."""
        return []

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
            edits.append(
                TabEdit(tab=label, diff=diff, original_snippet=original_snippet)
            )
            self.written[tab_id] = (label, current)
        return edits

    @staticmethod
    def has_meaningful_diff(original: str, current: str) -> bool:
        if not current.strip():
            return False
        return normalize_gdoc_text(original) != normalize_gdoc_text(current)


# ---------------------------------------------------------------------------
# Real-time GDoc sync helpers
# ---------------------------------------------------------------------------


def render_plan_progress(plan_path: Path) -> str:
    """Render the current plan JSON as markdown for the GDoc Plan tab."""
    if not plan_path.exists():
        return "# Planning...\n"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    parts: list[str] = []
    parts.append(f"# {data.get('title', 'Planning...')}\n")
    if thesis := data.get("thesis"):
        parts.append(f"**Thesis:** {thesis}\n")
    if direction := data.get("author_direction"):
        parts.append(f"**Author direction:** {direction}\n")
    sections = data.get("sections", [])
    if sections:
        parts.append("\n## Sections\n")
        for s in sections:
            parts.append(f"### {s.get('title', '???')}")
            parts.append(f"{s.get('summary', '')}\n")
            kp = s.get("key_points", [])
            if kp:
                parts.append("Key points: " + ", ".join(kp))
    questions = data.get("research_questions", [])
    if questions:
        parts.append("\n## Research Questions\n")
        for q in questions:
            parts.append(
                f"- [{q.get('priority', '?')}] {q.get('question', '')} "
                f"(for: {q.get('section', '')})"
            )
    quotes = data.get("source_quotes", [])
    if quotes:
        parts.append("\n## Source Quotes\n")
        for q in quotes:
            parts.append(f'- "{q.get("text", "")}" — {q.get("speaker", "")}')
    return "\n".join(parts)


def render_research_progress(research_path: Path) -> str:
    """Render the current research JSON as markdown for the GDoc Research tab."""
    if not research_path.exists():
        return "# Researching...\n"
    data = json.loads(research_path.read_text(encoding="utf-8"))
    findings = data.get("findings", [])
    parts = [f"# Research Findings\n\n{len(findings)} findings\n"]
    for f in findings:
        parts.append(f"\n## {f.get('question', '???')}\n")
        parts.append(f"{f.get('answer', '')}\n")
        parts.append(f"**Confidence:** {f.get('confidence', '?')}")
        sources = f.get("sources", [])
        if sources:
            urls = ", ".join(s.get("url", "") for s in sources if isinstance(s, dict))
            if urls:
                parts.append(f"\n**Sources:** {urls}")
    return "\n".join(parts)


class DraftSyncer:
    """Polls a draft file and pushes changes to a GDoc tab in real time.

    For small content, writes to a single tab. When content exceeds
    TAB_CONTINUATION_CHARS, splits across continuation tabs.
    """

    def __init__(
        self,
        doc_id: str,
        tab_id: str,
        file_path: Path,
        *,
        tab_name: str = "",
        session_state: WritingSessionState | None = None,
        interval: float = 5.0,
    ) -> None:
        self.doc_id = doc_id
        self.tab_id = tab_id
        self.tab_name = tab_name
        self.file_path = file_path
        self.session_state = session_state
        self.interval = interval
        self.last_content = ""
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.poll_loop())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.sync()

    async def poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.sync()

    async def sync(self) -> None:
        if not self.file_path.exists():
            return
        try:
            content = self.file_path.read_text(encoding="utf-8")
        except OSError:
            return
        if content == self.last_content or not content.strip():
            return
        self.last_content = content
        async with gdoc_nonfatal(f"sync draft {self.file_path.name}"):
            from inkwell.agent.tools.google_docs import TAB_CONTINUATION_CHARS

            if self.tab_name and len(content) > TAB_CONTINUATION_CHARS:
                from inkwell.agent.tools.google_docs import write_with_continuation

                await write_with_continuation(
                    self.doc_id,
                    self.tab_name,
                    content,
                    session_state=self.session_state,
                )
            else:
                await do_write_tab(
                    self.doc_id,
                    self.tab_id,
                    content,
                    session_state=self.session_state,
                )


# ---------------------------------------------------------------------------
# MCP server builders
# ---------------------------------------------------------------------------


def build_research_tools() -> list[LupMcpTool]:
    """Flat list of all research MCP tools (excludes fetch — now in source server)."""
    return [
        *EXA_TOOLS,
        *ARXIV_TOOLS,
        *FRED_TOOLS,
        *MARKET_TOOLS,
        *WIKIPEDIA_TOOLS,
    ]


def build_source_server() -> tuple[dict[str, McpServerConfig], list[str]]:
    """MCP server for source fetching/extraction — available to ALL pipeline stages."""
    tools = [*FETCH_TOOLS, *EXTRACT_MCP_TOOLS]
    server = create_mcp_server(
        name="source",
        version="1.0.0",
        tools=extract_sdk_tools(tools),
    )
    tool_names = [f"mcp__source__{t.sdk_tool.name}" for t in tools]
    return {"source": server}, tool_names


def build_compute_server(
    sandbox: Sandbox,
    artifacts_dir: Path,
) -> tuple[dict[str, McpServerConfig], list[str]]:
    """MCP server with code execution (sandbox) and artifact query tools.

    Returns (servers_dict, tool_name_list) ready to merge into any stage.
    """
    from inkwell.agent.tools.citations import CITATION_TOOLS

    tools = [*sandbox.create_tools(), *make_query_tools(artifacts_dir), *CITATION_TOOLS]
    server = create_mcp_server(
        name="compute",
        version="1.0.0",
        tools=extract_sdk_tools(tools),
    )
    tool_names = [f"mcp__compute__{t.sdk_tool.name}" for t in tools]
    return {"compute": server}, tool_names


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


def annotate_draft_with_findings(draft: str, findings: list[ReviewFinding]) -> str:
    """Inject review findings inline at matching text_excerpt locations."""
    annotated = draft

    anchorable = [
        f
        for f in findings
        if f.text_excerpt and f.severity in ("critical", "suggestion", "praise")
    ]
    anchorable.sort(key=lambda f: len(f.text_excerpt), reverse=True)

    anchored: set[int] = set()
    for f in anchorable:
        if f.severity == "praise":
            annotation = f"\n[PRESERVE:{f.reviewer}] {f.issue}\n"
        else:
            annotation = (
                f"\n[FINDING:{f.severity}:{f.reviewer}] {f.issue}\n  → {f.suggestion}\n"
            )
        if f.text_excerpt in annotated:
            annotated = annotated.replace(  # claude: ignore
                f.text_excerpt,
                f"{f.text_excerpt}{annotation}",
                1,
            )
            anchored.add(id(f))

    remaining = [
        f
        for f in findings
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


async def extract_single_source(url: str, existing_doc_id: str | None) -> str:
    """Dispatch a single URL to its deterministic extractor. Returns markdown text."""
    if "claude.ai/share" in url:
        result = await do_extract_conversation(url)
        return Path(result.content.path).read_text(encoding="utf-8")

    if GDOC_URL_PATTERN.search(url):
        doc_id = parse_gdoc_id(url)
        if existing_doc_id and doc_id == existing_doc_id:
            return await extract_source_tab_only(url)
        result = await do_extract_gdoc(url)
        return "\n\n".join(
            Path(tab.content.path).read_text(encoding="utf-8") for tab in result.tabs
        )

    if "lesswrong.com/posts" in url:
        result = await do_extract_lesswrong(url)
        return Path(result.content.path).read_text(encoding="utf-8")

    if url.startswith(("/", "~", "./")):
        result = await do_extract_file(url)
        return Path(result.content.path).read_text(encoding="utf-8")

    result = await do_fetch_source(url)
    return Path(result.content.path).read_text(encoding="utf-8")


async def extract_with_agent_fallback(
    failed_urls: list[str],
    source_dir: Path,
    *,
    source_servers: dict[str, McpServerConfig],
    source_tool_names: list[str],
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[str]:
    """Fall back to an agent for URLs that failed deterministic extraction."""
    source_list = "\n".join(f"- {url}" for url in failed_urls)
    task = (
        f"These sources failed automatic extraction. Try alternatives:\n"
        f"1. Use exa_search to find the content elsewhere\n"
        f"2. Try a different URL format (remove tracking params, try archive)\n"
        f"3. Search for the page title\n\n"
        f"Sources:\n{source_list}\n\n"
        f"Write extracted content to: {source_dir / 'recovered.md'}\n"
        f"Separate each with '--- Source: <url> ---' headers."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=EXTRACTOR_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=source_servers,
        allowed_tools=source_tool_names,
        prefix="[extract:fallback] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    recovered_path = source_dir / "recovered.md"
    if recovered_path.exists():
        return [recovered_path.read_text(encoding="utf-8")]
    return []


async def plan_article(
    notes: PipelineNotes,
    *,
    target_format: str = "auto",
    voice_file_paths: list[str] | None = None,
    source_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    author_directions: str = "",
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    on_plan_update: Callable[[], Awaitable[None]] | None = None,
) -> ArticlePlan:
    """Stage 1: Extract a structured article plan from source material."""
    plan_path = notes.artifact_path("plan")
    collector = PlanCollector(plan_path, on_save=on_plan_update)
    output_servers, output_tool_names = build_output_server(
        "output", make_plan_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("plan", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    from inkwell.agent.stages import FORMAT_KEYS

    format_list = ", ".join(FORMAT_KEYS)
    format_hint = (
        f"Choose the best output format for the content: {format_list}"
        if target_format == "auto"
        else f"Suggested format: {target_format} (override if content is better "
        f"suited to another format: {format_list})"
    )

    format_guidance = get_format_guidance(target_format)
    conversation_path = notes.text_artifact_path("conversation")

    manifest = ContentManifest()
    manifest.add(
        conversation_path,
        "source",
        "Source material",
        instruction="extract article plan from this",
    )
    for p in source_file_paths or []:
        if p != str(conversation_path):
            manifest.add(
                p, "source", Path(p).stem, instruction="additional source material"
            )
    add_voice_refs(manifest, voice_file_paths or [])

    task = (
        f"Extract a structured article plan from the source material.\n"
        f"{format_hint}\n\n"
        f"{manifest.render()}\n\n"
    )
    if author_directions:
        task += (
            f"The author left comments on the source document before this pipeline "
            f"started. Treat these as directions that should guide the plan:\n\n"
            f"{author_directions}\n\n"
        )
    if format_guidance:
        task += f"{format_guidance}\n\n"
    task += (
        "Read the file, then build the plan using set_plan_header, "
        "add_section, add_research_question, and add_source_quote."
    )

    all_servers = {
        **output_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        output_tool_names + (source_tool_names_list or []) + (compute_tool_names or [])
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=PLANNER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
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
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    on_plan_update: Callable[[], Awaitable[None]] | None = None,
) -> ArticlePlan:
    """Refine the initial plan using research findings."""
    plan_path = notes.artifact_path("plan")
    refined_path = notes.artifacts_dir / "plan_refined.json"
    collector = PlanCollector(refined_path, on_save=on_plan_update)
    output_servers, output_tool_names = build_output_server(
        "output", make_plan_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("refine", note_collector)
    output_servers = {**output_servers, **note_servers}
    output_tool_names = output_tool_names + note_tool_names

    manifest = ContentManifest()
    manifest.add(plan_path, "plan", "Current plan")
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    task = (
        f"Refine the article plan using the research findings.\n\n"
        f"{manifest.render()}\n"
        f"Use list_research to browse all research findings, then read_finding for details.\n\n"
        f"Read the plan, then build the refined plan using "
        f"set_plan_header, add_section, add_research_question, and add_source_quote."
    )

    all_servers = {
        **output_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        output_tool_names + (source_tool_names_list or []) + (compute_tool_names or [])
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=REFINER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        prefix="[refine] ",
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    if not refined_path.exists():
        return notes.load_artifact("plan", ArticlePlan) or ArticlePlan(
            title="Untitled",
            thesis="",
            target_format="auto",
            sections=[],
            research_questions=[],
            source_quotes=[],
            author_direction="",
            voice_notes="",
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
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    on_research_update: Callable[[], Awaitable[None]] | None = None,
) -> ResearchCompilation:
    """Stage 2: Deep research on all questions from the article plan."""
    if servers is None:
        servers = build_research_servers()

    plan_path = notes.artifact_path("plan")
    research_path = notes.artifact_path("research")
    collector = ResearchCollector(research_path, on_save=on_research_update)
    output_servers, output_tool_names = build_output_server(
        "output", make_research_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("research", note_collector)
    all_servers = {
        **servers,
        **output_servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        research_tool_names()
        + output_tool_names
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

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
        max_thinking_tokens=128_000 - 1,
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


async def research_questions(
    notes: PipelineNotes,
    questions: list[str],
    *,
    servers: dict[str, McpServerConfig] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ResearchCompilation:
    """Research specific questions, appending findings to the research artifact.

    Used by restarts: the orchestrator's RewriteActions carry new research
    questions whose answers must land before section rewrites read them.
    """
    if servers is None:
        servers = build_research_servers()

    research_path = notes.artifact_path("research")
    collector = ResearchCollector(research_path)
    if research_path.exists():
        existing = json.loads(research_path.read_text(encoding="utf-8"))
        collector.findings = list(existing.get("findings", []))
        collector.suggested_additions = list(existing.get("suggested_additions", []))
        collector.additional_context = str(existing.get("additional_context", ""))

    output_servers, output_tool_names = build_output_server(
        "output", make_research_output_tools(collector)
    )
    all_servers = {
        **servers,
        **output_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        research_tool_names()
        + output_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

    question_list = "\n".join(f"- {q}" for q in questions)
    task = (
        f"Research these questions raised by author feedback:\n\n"
        f"{question_list}\n\n"
        f"Investigate each one, then call record_finding for each answer."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=RESEARCHER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[research:restart] ",
        cost_accumulator=cost_accumulator,
    )
    return ResearchCompilation.model_validate_json(
        research_path.read_text(encoding="utf-8")
    )


async def write_section(
    section_title: str,
    *,
    notes: PipelineNotes,
    draft_path: Path,
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    section_context: str = "",
    target_format: str = "auto",
    servers: dict[str, McpServerConfig] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> SectionDraft:
    """Write a single section using built-in Write. Called in parallel."""
    if servers is None:
        servers = build_research_servers()

    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server(
        f"write:{section_title}", note_collector
    )
    all_servers = {
        **servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        research_tool_names()
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

    plan_path = notes.artifact_path("plan")

    manifest = ContentManifest()
    manifest.add(plan_path, "plan", "Article plan")
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = (
        f'Write the section "{section_title}" for the article.\n\n'
        f"{manifest.render()}\n"
        f"Use list_research to browse all research findings, then read_finding for details.\n\n"
    )
    if format_guidance:
        task += f"{format_guidance}\n\n"
    if section_context:
        task += f"{section_context}\n\n"
    task += (
        f"Read the plan to find this section's details (summary, key points, "
        f"quotes to include). Use list_research and read_finding for research. "
        f"Read each voice analysis and style reference file to match "
        f"the author's style and apply all hard editing rules.\n\n"
        f"Write the complete section to: {draft_path}\n\n"
        f"Use the Write tool to write the file. Write the entire section "
        f"in one call. If you have questions for the author, call note_for_author."
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=SECTION_WRITER_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
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
        raise PipelineError(f"Section writer for '{section_title}' produced no output")

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
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
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
        f"missing transitions, redundant openings, and structural issues.\n"
        f"Use list_research to browse all findings, then read_finding for details on any section.\n\n"
        f"Write the merge plan to: {output_path}"
    )

    all_servers = {**(source_servers or {}), **(compute_servers or {})}
    all_tools = (source_tool_names_list or []) + (compute_tool_names or [])

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=MERGE_PLAN_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
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
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    target_format: str = "auto",
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
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
        source_servers=source_servers,
        source_tool_names_list=source_tool_names_list,
        compute_servers=compute_servers,
        compute_tool_names=compute_tool_names,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )

    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("merge", note_collector)
    merge_servers = {
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    merge_tools = (
        note_tool_names + (source_tool_names_list or []) + (compute_tool_names or [])
    )

    plan_path = notes.artifact_path("plan")
    manifest = ContentManifest()
    manifest.add(
        merge_plan_path, "merge_plan", "Merge plan", instruction="read this FIRST"
    )
    for title, path in section_draft_paths.items():
        manifest.add(
            path, "draft", f"Section: {title}", instruction="read each individually"
        )
    manifest.add(plan_path, "plan", "Article plan")
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = f"Rewrite these sections into a unified piece.\n\n{manifest.render()}\n\n"
    if format_guidance:
        task += f"{format_guidance}\n\n"
    task += (
        f"Read the merge plan FIRST — it contains a target outline that "
        f"defines your output's structure paragraph by paragraph. Then read "
        f"each section draft file individually. Follow the outline, not the "
        f"input section boundaries. Read each voice and style reference file "
        f"to match the author's voice and apply hard editing rules.\n\n"
        f"Write the complete merged draft to: {output_path}"
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=COHERENCE_EDITOR_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=merge_servers,
        allowed_tools=merge_tools,
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
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for narrative coherence."""
    plan_path = notes.artifact_path("plan")
    review_path = notes.artifacts_dir / "review_narrative.json"
    collector = ReviewCollector(review_path, "narrative")
    output_servers, output_tool_names = build_output_server(
        "output", make_review_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server(
        "review:narrative", note_collector
    )
    all_servers = {
        **output_servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        output_tool_names
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

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
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
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
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
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
    all_servers = {
        **servers,
        **output_servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        review_tool_names()
        + output_tool_names
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

    task = (
        f"Fact-check every verifiable claim in the article draft.\n\n"
        f"Draft: {draft_path}\n\n"
        f"Read the draft. Use list_research to see all research findings, "
        f"then read_finding for details on specific ones. Cross-reference "
        f"the draft against research findings, then verify remaining claims "
        f"with your research tools. Call record_finding for each issue."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=FACT_CHECKER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
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
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for writing quality and voice consistency."""
    review_path = notes.artifacts_dir / "review_style.json"
    collector = ReviewCollector(review_path, "style")
    output_servers, output_tool_names = build_output_server(
        "output", make_review_output_tools(collector)
    )
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("review:style", note_collector)
    all_servers = {
        **output_servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        output_tool_names
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

    manifest = ContentManifest()
    manifest.add(draft_path, "draft", "Article draft")
    add_voice_refs(manifest, voice_file_paths or [])

    task = (
        f"Review the article draft for writing quality and style.\n\n"
        f"{manifest.render()}\n\n"
        f"Read each voice and style reference file, then read the draft. "
        f"Call record_finding for each issue."
    )
    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=STYLE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
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
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[ReviewFinding]:
    """Stage 5: Run all three reviewers in parallel."""
    narrative_task = review_narrative(
        notes,
        draft_path,
        author_notes=author_notes,
        source_servers=source_servers,
        source_tool_names_list=source_tool_names_list,
        compute_servers=compute_servers,
        compute_tool_names=compute_tool_names,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    facts_task = review_facts(
        notes,
        draft_path,
        servers=servers,
        author_notes=author_notes,
        source_servers=source_servers,
        source_tool_names_list=source_tool_names_list,
        compute_servers=compute_servers,
        compute_tool_names=compute_tool_names,
        trace_logger=trace_logger,
        cost_accumulator=cost_accumulator,
    )
    style_task = review_style(
        notes,
        draft_path,
        voice_file_paths=voice_file_paths,
        author_notes=author_notes,
        source_servers=source_servers,
        source_tool_names_list=source_tool_names_list,
        compute_servers=compute_servers,
        compute_tool_names=compute_tool_names,
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


class ConsolidatedFindings(NamedTuple):
    findings: list[ReviewFinding]
    dropped_suggestions: int


def consolidate_findings(
    findings: list[ReviewFinding],
    max_suggestions: int = 15,
) -> ConsolidatedFindings:
    """Deduplicate and budget review findings to prevent cumulative smoothing."""
    critical: list[ReviewFinding] = []
    praise: list[ReviewFinding] = []
    suggestions: list[ReviewFinding] = []

    seen_excerpts: dict[str, ReviewFinding] = {}

    for f in findings:
        if f.severity == "critical":
            critical.append(f)
            continue
        if f.severity == "praise":
            praise.append(f)
            continue
        if f.text_excerpt:
            key = f.text_excerpt[:200]
            if key in seen_excerpts:
                existing = seen_excerpts[key]
                existing.suggestion = (
                    f"{existing.suggestion}\n[Also from {f.reviewer}]: {f.suggestion}"
                )
                continue
            seen_excerpts[key] = f
        suggestions.append(f)

    anchored = [s for s in suggestions if s.text_excerpt]
    unanchored = [s for s in suggestions if not s.text_excerpt]
    ranked = anchored + unanchored
    capped = ranked[:max_suggestions]
    dropped = len(ranked) - len(capped)

    if dropped > 0:
        logger.info("Capped suggestions from %d to %d", len(ranked), max_suggestions)

    return ConsolidatedFindings(
        findings=critical + capped + praise,
        dropped_suggestions=dropped,
    )


async def rewrite_final(
    notes: PipelineNotes,
    draft_path: Path,
    findings: list[ReviewFinding],
    *,
    output_path: Path,
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    target_format: str = "auto",
    dropped_suggestions: int = 0,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Stage 6: Incorporate all feedback and produce the final article."""
    note_collector = author_notes if author_notes is not None else []
    note_servers, note_tool_names = build_note_server("rewrite", note_collector)
    rewrite_servers = {
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    rewrite_tools = (
        note_tool_names + (source_tool_names_list or []) + (compute_tool_names or [])
    )
    plan_path = notes.artifact_path("plan")

    draft_content = draft_path.read_text(encoding="utf-8")
    annotated = annotate_draft_with_findings(draft_content, findings)
    annotated_path = notes.artifacts_dir / "draft_annotated.md"
    annotated_path.write_text(annotated, encoding="utf-8")

    review_summary_path = notes.artifacts_dir / "review_summary.md"
    n_critical = sum(1 for f in findings if f.severity == "critical")
    n_suggestion = sum(1 for f in findings if f.severity == "suggestion")
    n_total = n_critical + n_suggestion
    summary_lines = [
        "# Review Summary\n",
        f"**{n_critical} critical** (mandatory), **{n_suggestion} suggestions** "
        f"({n_total} total findings)\n",
        "Review findings are annotated inline in the draft file.\n",
    ]
    if dropped_suggestions > 0:
        summary_lines.append(
            f"\n**Note:** {dropped_suggestions} lower-priority suggestion(s) were "
            f"omitted to keep the rewrite focused. All critical findings are included.\n"
        )
    review_summary_path.write_text("".join(summary_lines), encoding="utf-8")

    manifest = ContentManifest()
    manifest.add(
        annotated_path,
        "draft",
        "Annotated draft",
        instruction="review findings are marked inline",
    )
    manifest.add(plan_path, "plan", "Article plan")
    manifest.add(review_summary_path, "review", "Review summary")
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = (
        f"Produce the final version of this piece.\n\n"
        f"Target format: {target_format}\n\n"
    )
    if format_guidance:
        task += f"{format_guidance}\n\n"
    task += (
        f"{manifest.render()}\n\n"
        f"Read the annotated draft — review findings are marked inline. "
        f"Passages marked [PRESERVE:] were praised by reviewers: protect "
        f"their quality while editing around them. "
        f"Apply all critical findings and worthwhile suggestions. "
        f"Use list_research + read_finding to verify corrections against research findings. "
        f"If a style rules file is available, read it and enforce every "
        f"hard editing rule with zero remaining violations.\n\n"
        f"Write the final piece to: {output_path}"
    )

    collector = await query(
        task,
        model="claude-opus-4-6",
        system_prompt=REWRITER_SYSTEM,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=rewrite_servers,
        allowed_tools=rewrite_tools,
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
            result = do_format_lesswrong(FormatLesswrongInput(content=content))
            return result.content
        case "twitter":
            result = await do_format_twitter(FormatTwitterInput(content=content))
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
            result = await do_format_dialog(FormatDialogInput(content=content))
            return result.compiled
        case "memo":
            from inkwell.agent.tools.formats import FormatMemoInput, do_format_memo

            result = await do_format_memo(FormatMemoInput(content=content, title=title))
            return result.content
        case "academic":
            from inkwell.agent.tools.formats import (
                FormatAcademicInput,
                do_format_academic,
            )

            result = await do_format_academic(
                FormatAcademicInput(content=content, title=title)
            )
            return result.content
        case "newsletter":
            from inkwell.agent.tools.formats import (
                FormatNewsletterInput,
                do_format_newsletter,
            )

            result = do_format_newsletter(
                FormatNewsletterInput(content=content, title=title)
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
    source_servers: dict[str, McpServerConfig] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerConfig] | None = None,
    compute_tool_names: list[str] | None = None,
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

    research_hint = ""
    if has_research:
        research_hint = (
            "Use list_research and read_finding to check research findings.\n"
        )

    task = (
        f"Review the article plan and surface all uncertainties.\n\n"
        f"{file_refs}\n"
        f"{research_hint}"
        f"Read the plan, then call record_assumption for each uncertainty."
    )

    all_servers = {
        **output_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    all_tools = (
        output_tool_names + (source_tool_names_list or []) + (compute_tool_names or [])
    )

    await query(
        task,
        model="claude-opus-4-6",
        system_prompt=ASSUMPTIONS_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        permission_mode="bypassPermissions",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
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
    if result.items:
        specs = [
            CommentSpec(
                content=(
                    f"{tag_prefix.get(item.tag, '[NOTE]')} {item.content}\n\n"
                    f"My best guess: {item.best_guess}"
                ),
                anchor_text=item.anchor_section,
            )
            for item in result.items
        ]
        async with gdoc_nonfatal("post assumption comments"):
            await do_insert_comments_batch(
                doc_id,
                specs,
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

    sections_text = "\n".join(f"- {s.title}: {s.summary}" for s in plan.sections)

    task = (
        f"Determine how to handle author feedback for this article.\n\n"
        f"# Current Plan: {plan.title}\n\n"
        f"**Thesis:** {plan.thesis}\n\n"
        f"## Sections\n{sections_text}\n\n"
    )
    if snapshot.section_drafts:
        drafts_text = "\n\n---\n\n".join(
            f"## {title}\n\n{draft.content}"
            for title, draft in snapshot.section_drafts.items()
        )
        drafts_path = notes.artifacts_dir / "orchestrator_drafts.md"
        drafts_path.write_text(drafts_text, encoding="utf-8")
        task += f"## Current Drafts\n\nRead the current drafts from: {drafts_path}\n\n"
    task += f"{feedback_text}"

    strategy = await query(
        task,
        output_type=RestartStrategy,
        model="claude-opus-4-6",
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=BUILTIN_READ_TOOLS,
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
    """State-machine wrapper around the pipeline stages."""

    def __init__(
        self,
        *,
        sources: list[str],
        refs: list[str] | None = None,
        target_format: str = "auto",
        existing_doc_id: str | None = None,
        session_state: WritingSessionState | None = None,
        notes: PipelineNotes | None = None,
        trace_logger: TraceLogger | None = None,
        listener: PipelineListener | None = None,
        cost_accumulator: CostAccumulator | None = None,
    ) -> None:
        self.sources = sources
        self.refs = refs or []
        self.target_format = target_format
        self.existing_doc_id = existing_doc_id
        self.state = session_state or WritingSessionState()
        self.notes = notes
        self.trace_logger = trace_logger
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
        self.source_servers, self.source_tool_names = build_source_server()

        self.sandbox: Sandbox | None = None
        self.compute_servers: dict[str, McpServerConfig] = {}
        self.compute_tool_names: list[str] = []

        self.doc_id = ""
        self.doc_url = ""
        self.known_tabs: dict[str, str] = {}
        self.tab_ids: dict[str, str] = {}
        self.tabs: TabTracker | None = None

        self.overview_tab_id = ""
        self.draft_tab_id = ""
        self.final_tab_id = ""

        self.watcher: BackgroundAgent | None = None
        self.source_watcher: BackgroundAgent | None = None
        self.source_is_extracted_gdoc = False

    @property
    def effective_format(self) -> str:
        plan = self.snapshot.plan
        if plan and plan.target_format and plan.target_format != "auto":
            return plan.target_format
        return self.target_format

    def ensure_notes(self) -> PipelineNotes:
        """Return notes, creating a temp dir if needed."""
        if self.notes is None:
            self.notes = PipelineNotes(Path(tempfile.mkdtemp(prefix="inkwell-notes-")))
        from lup.content_safety import configure_content_safety

        configure_content_safety(self.notes.base_dir / "content")
        return self.notes

    def start_sandbox(self) -> None:
        """Start the Docker sandbox and build compute MCP servers."""
        notes = self.ensure_notes()
        shared_dir = notes.artifacts_dir / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox = Sandbox(
            session_id=f"inkwell-{id(self)}",
            shared_dir=shared_dir,
        )
        try:
            self.sandbox.start()
        except Exception:
            logger.warning("Sandbox unavailable — running without code execution")
            self.sandbox = None
            from inkwell.agent.tools.citations import CITATION_TOOLS

            self.compute_servers, self.compute_tool_names = build_output_server(
                "compute",
                [*make_query_tools(notes.artifacts_dir), *CITATION_TOOLS],
            )
            return

        self.compute_servers, self.compute_tool_names = build_compute_server(
            self.sandbox, notes.artifacts_dir
        )
        if self.state is not None:
            self.state.shared_dir = shared_dir
        logger.info(
            "Sandbox started with %d compute tools", len(self.compute_tool_names)
        )

    def stop_sandbox(self) -> None:
        if self.sandbox is not None:
            self.sandbox.stop()
            self.sandbox = None
            logger.info("Sandbox stopped")

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
        self.snapshot.source_doc_id = self.state.source_doc_id
        self.snapshot.seen_comment_ids = set(self.state.seen_comment_ids)
        self.snapshot.agent_comment_ids = set(self.state.agent_comment_ids)
        self.snapshot.seen_source_comment_ids = set(self.state.seen_source_comment_ids)
        self.snapshot.pending_questions = list(self.state.pending_questions)

        notes = self.ensure_notes()
        data = self.snapshot.model_dump_json()
        base = notes.base_dir
        base.mkdir(parents=True, exist_ok=True)
        (base / "snapshot.json").write_text(data, encoding="utf-8")
        (base / f"snapshot_{self.snapshot.stage}.json").write_text(
            data, encoding="utf-8"
        )

    def install_block_callback(self) -> None:
        """Set the contextvar so all query() calls forward blocks to the listener."""
        from claude_agent_sdk import ContentBlock

        async def forward_block(block: ContentBlock, prefix: str) -> None:
            info = extract_block_info(block)
            await self.hooks.on_block(info.label, info.content, prefix)

        active_block_callback.set(forward_block)

    async def run(self) -> WritingOutput:
        """Execute the full pipeline with restart support."""
        self.install_block_callback()
        self.snapshot.profile = config_mod.settings.profile
        configure_session_state(self.state)
        await self.setup_doc()
        self.start_sandbox()

        try:
            await self.stage_preprocess()
            await self.stage_extract()
            await self.stage_voice()
            await self.stage_plan()
            await self.sync_if_requested()

            await self.stage_research()
            await self.check_and_maybe_restart()
            await self.sync_if_requested()

            await self.stage_assumptions()
            await self.stage_refine()

            self.start_watcher()

            try:
                await self.stage_write()
                await self.check_and_maybe_restart()
                await self.sync_if_requested()

                await self.stage_merge()
                await self.sync_if_requested()
                await self.stage_review()
                await self.check_and_maybe_restart()
                await self.sync_if_requested()

                await self.stage_rewrite()
                await self.sync_if_requested()
                await self.stage_format()

                output = self.snapshot.output
                if output is None:
                    raise PipelineError("Pipeline completed without producing output")

                await self.update_overview()
                await self.hooks.on_complete(output)

                await self.standby_loop()
            finally:
                await self.stop_watcher()
        finally:
            self.stop_sandbox()

        output = self.snapshot.output
        if output is None:
            raise PipelineError("Pipeline completed without producing output")
        return output

    async def run_from(self, snapshot: PipelineSnapshot) -> WritingOutput:
        """Resume pipeline execution from a saved snapshot."""
        self.install_block_callback()
        self.snapshot = snapshot
        configure_session_state(self.state)

        if snapshot.doc_id:
            self.existing_doc_id = snapshot.doc_id
        elif snapshot.output and snapshot.output.google_doc_id:
            self.existing_doc_id = snapshot.output.google_doc_id

        self.state.seen_comment_ids = set(snapshot.seen_comment_ids)
        self.state.agent_comment_ids = set(snapshot.agent_comment_ids)
        self.state.seen_source_comment_ids = set(snapshot.seen_source_comment_ids)
        self.state.source_doc_id = snapshot.source_doc_id
        self.state.pending_questions = list(snapshot.pending_questions)

        await self.setup_doc()
        self.start_sandbox()

        try:
            stages = [
                "preprocess",
                "extract",
                "voice",
                "plan",
                "research",
                "assumptions",
                "refine",
                "write",
                "merge",
                "review",
                "rewrite",
                "format",
            ]
            last_idx = stages.index(snapshot.stage) if snapshot.stage in stages else -1
            remaining = stages[last_idx + 1 :]

            if snapshot.plan:
                self.start_watcher()

            try:
                for stage_name in remaining:
                    method = getattr(self, f"stage_{stage_name}")
                    await method()
                    if stage_name in ("research", "write", "review"):
                        await self.check_and_maybe_restart()
                    await self.sync_if_requested()

                output = self.snapshot.output
                if output is None:
                    raise PipelineError("Pipeline completed without producing output")

                await self.update_overview()
                await self.hooks.on_complete(output)
                await self.standby_loop()
            finally:
                await self.stop_watcher()
        finally:
            self.stop_sandbox()

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

        if self.state.source_doc_id and not self.source_is_extracted_gdoc:
            self.source_watcher = create_source_watcher(
                session_state=self.state,
                notes=self.notes,
                plan_breaking_signal=self.plan_breaking,
            )
            self.source_watcher.start()
            logger.info("Source document watcher started")

    async def stop_watcher(self) -> None:
        if self.source_watcher is not None:
            await self.source_watcher.stop()
            self.source_watcher = None
            logger.info("Source document watcher stopped")
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
                f"Inkwell — {', '.join(s[:30] for s in self.sources)[:60]}",
                share_with=config_mod.settings.author_email,
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
                    f"**Sources:** {', '.join(self.sources)[:120]}\n\n"
                    f"**Stage:** starting\n\n"
                    f"*Comment on any tab to give feedback. "
                    f"Edit directly to override agent decisions.*"
                ),
                session_state=self.state,
            )

    async def gather_feedback(self) -> int:
        """Ingest new author feedback from every channel.

        GDoc comments, environment input, and tab edits all flow through
        the same classify-and-record path, so impact classification (and
        plan-breaking detection) does not depend on which poller sees an
        item first. Returns the number of new items ingested.
        """
        count = 0

        for comment in await self.state.get_new_author_comments():
            await self.classify_and_record_comment(comment)
            count += 1

        for text in await self.hooks.collect_author_input(self.state):
            await self.classify_and_record_terminal(text)
            count += 1

        if self.tabs is not None:
            for edit in await self.tabs.detect_edits():
                await self.classify_and_record_edit(edit)
                count += 1

        return count

    async def classify_and_record_comment(self, comment: AuthorComment) -> None:
        """Classify a GDoc comment, record it, acknowledge it on the doc."""
        if comment["reply"]:
            display = f'Author reply to "{comment["content"]}": {comment["reply"]}'
        else:
            display = comment["content"]
            if comment["anchor_text"]:
                display += f' (on: "{comment["anchor_text"]}")'
        await self.hooks.on_message("gdoc", display)

        task = f"Classify this author comment:\n\nComment: {comment['content']}\n"
        if comment["anchor_text"]:
            task += f'Anchored to: "{comment["anchor_text"]}"\n'
        if comment["reply"]:
            task += f"Reply to agent question: {comment['reply']}\n"

        classified = await query(
            task,
            output_type=ClassifiedComment,
            model="claude-opus-4-6",
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            permission_mode="bypassPermissions",
            prefix="[classify-comment] ",
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        if classified is None:
            classified = ClassifiedComment(
                comment_id="",
                content="",
                impact="stage_local",
            )
        classified.comment_id = comment["comment_id"]
        classified.content = comment["content"]
        classified.anchor_text = comment["anchor_text"]
        classified.reply = comment["reply"]

        if classified.impact == "dismiss":
            return

        notes = self.ensure_notes()
        await notes.add_comment(classified)

        reply_text = ACKNOWLEDGE_TEMPLATES.get(classified.impact)
        if reply_text:
            async with gdoc_nonfatal("acknowledge comment"):
                await do_reply_to_comment(
                    self.doc_id, comment["comment_id"], reply_text
                )

        if classified.impact == "plan_breaking":
            logger.info("Plan-breaking GDoc comment")
            self.plan_breaking.set()

    async def post_author_notes(self) -> None:
        """Post any new author notes as GDoc comments, then clear the list."""
        if not self.author_notes:
            return
        notes_to_post = list(self.author_notes)
        self.author_notes.clear()
        specs = [
            CommentSpec(
                content=f"[{note.stage.upper()}] {note.note}"
                if note.stage
                else f"[NOTE] {note.note}",
                anchor_text=note.anchor or None,
            )
            for note in notes_to_post
        ]
        async with gdoc_nonfatal("post author notes"):
            await do_insert_comments_batch(
                self.doc_id,
                specs,
                session_state=self.state,
            )
        for note in notes_to_post:
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
            max_thinking_tokens=128_000 - 1,
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
                snippet = (
                    edit.original_snippet[:500] if edit.original_snippet else edit.diff
                )
                await do_insert_comment(
                    self.doc_id,
                    (
                        f"[REVERT CHECK] This edit on '{edit.tab}' looks like it "
                        f"might be accidental. Did you mean to make this change?\n\n"
                        f"Original text:\n{snippet}\n\n"
                        f"(Reply 'yes' to keep the edit, or 'no' / ignore to revert.)"
                    ),
                    anchor_text=edit.original_snippet[:200]
                    if edit.original_snippet
                    else None,
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

    async def classify_and_record_terminal(self, text: str) -> None:
        classified = await query(
            f"Classify this terminal direction from the author:\n\n{text}",
            output_type=ClassifiedComment,
            model="claude-opus-4-6",
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            permission_mode="bypassPermissions",
            prefix="[classify-terminal] ",
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        if classified is None:
            notes = self.ensure_notes()
            await notes.add_terminal_input(text)
            return

        if classified.impact == "dismiss":
            return

        classified.comment_id = f"terminal-{uuid.uuid4().hex[:8]}"
        classified.content = text
        classified.tags = [*(classified.tags or []), "terminal"]

        notes = self.ensure_notes()
        await notes.add_comment(classified)

        if classified.impact == "plan_breaking":
            logger.info("Plan-breaking terminal input")
            self.plan_breaking.set()

    async def prepare_feedback(self, stage: str) -> Path | None:
        """Ingest new feedback and render the accumulated set to a file."""
        await self.gather_feedback()
        notes = self.ensure_notes()
        if not await notes.get_all_feedback():
            return None
        return await notes.render_feedback_file(stage)

    async def collect_preexisting_directions(self, doc_id: str) -> int:
        """Collect pre-existing comments from the source GDoc as author directions.

        These comments predate the pipeline — they're input context (like the
        conversation itself), not live feedback. Stored as a markdown file in
        notes/directions/ and injected into the plan stage prompt. The source
        GDoc is never modified.

        Returns the number of comments collected.
        """
        try:
            comments = await fetch_gdoc_comments(doc_id)
        except (RuntimeError, OSError) as exc:
            logger.warning("Could not fetch source doc comments: %s", exc)
            return 0

        if not comments:
            return 0

        lines: list[str] = ["# Author Directions", ""]
        lines.append(
            "Pre-existing comments from the source document. "
            "Treat these as author directions that should guide "
            "the article's structure, tone, and content.\n"
        )

        for comment in comments:
            entry = f"- **{comment.author}**: {comment.content}"
            if comment.anchor_text:
                entry += f'  \n  *On: "{comment.anchor_text}"*'
            if comment.replies:
                for reply in comment.replies:
                    entry += f"\n  - Reply: {reply}"
            lines.append(entry)

        notes = self.ensure_notes()
        rendered = "\n".join(lines)
        notes.save_directions(rendered)
        logger.info(
            "Saved %d pre-existing comments as author directions", len(comments)
        )
        return len(comments)

    async def stage_preprocess(self) -> None:
        """Classify sources by role and extract author instructions."""
        await self.hooks.on_stage("preprocess", "Classifying sources")

        from inkwell.agent.tools.preprocess import preprocess_sources

        result = await preprocess_sources(self.sources)
        notes = self.ensure_notes()
        notes.save_artifact("preprocess", result)

        self.snapshot.raw_sources = result.raw_inputs
        self.snapshot.author_instructions = result.instructions
        self.snapshot.runtime_style_refs = result.style_refs
        self.snapshot.stage = "preprocess"

        routed_sources = result.sources
        if routed_sources:
            self.sources = routed_sources
        self.refs = list(set(self.refs + result.style_refs + result.context_refs))

        await self.save_snapshot()

        parts: list[str] = []
        if result.instructions:
            parts.append("instructions extracted")
        parts.append(f"{len(result.sources)} source(s)")
        if result.style_refs:
            parts.append(f"{len(result.style_refs)} style ref(s)")
        if result.context_refs:
            parts.append(f"{len(result.context_refs)} context ref(s)")
        await self.hooks.on_progress(f"Preprocess: {', '.join(parts)}")

    async def stage_extract(self) -> None:
        await self.hooks.on_stage("extract", "Extracting source material")
        await self.update_overview(active_stage="extract")

        notes = self.ensure_notes()
        source_dir = notes.artifacts_dir / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        output_path = source_dir / "conversation.md"

        source_doc_id = ""
        for src in self.sources:
            if not source_doc_id and GDOC_URL_PATTERN.search(src):
                source_doc_id = parse_gdoc_id(src)
                self.state.source_doc_id = source_doc_id
                self.source_is_extracted_gdoc = True

        all_urls = list(self.sources) + list(self.refs)
        results = await asyncio.gather(
            *(extract_single_source(url, self.existing_doc_id) for url in all_urls),
            return_exceptions=True,
        )

        extracted_parts: list[str] = []
        failed: list[str] = []
        for url, result in zip(all_urls, results):
            if isinstance(result, Exception):
                logger.warning("Extraction failed for %s: %s", url, result)
                failed.append(url)
            elif result:
                extracted_parts.append(f"--- Source: {url} ---\n\n{result}")

        if failed:
            await self.hooks.on_progress(
                f"Retrying {len(failed)} failed source(s) with agent fallback"
            )
            recovered = await extract_with_agent_fallback(
                failed,
                source_dir,
                source_servers=self.source_servers,
                source_tool_names=self.source_tool_names,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
            extracted_parts.extend(recovered)

        conversation = "\n\n".join(extracted_parts)
        output_path.write_text(conversation, encoding="utf-8")

        all_source_paths = [str(p) for p in sorted(source_dir.glob("*")) if p.is_file()]

        self.snapshot.conversation = conversation
        self.snapshot.source_file_paths = all_source_paths
        self.snapshot.stage = "extract"
        await notes.save_content(
            "conversation",
            conversation,
            stage="extract",
            content_type="source",
        )
        await self.save_snapshot()

        if source_doc_id:
            n = await self.collect_preexisting_directions(source_doc_id)
            if n:
                await self.hooks.on_progress(
                    f"Collected {n} pre-existing comments as author directions"
                )

        async with gdoc_nonfatal("write source tab"):
            source_tab_id = self.known_tabs["Source"]
            gdoc_text = conversation
            await do_write_tab(
                self.doc_id, source_tab_id, gdoc_text, session_state=self.state
            )
            assert self.tabs is not None
            await self.tabs.record_from_doc(source_tab_id, "Source")
        await self.update_overview()

        if all_source_paths:
            n_files = len(all_source_paths)
            await self.hooks.on_progress(f"Extract: {n_files} source file(s) saved")

    async def stage_voice(self) -> None:
        await self.hooks.on_stage("voice", "Analyzing author's writing voice")
        await self.update_overview(active_stage="voice")
        corpus_samples, corpus_sources, corpus_types = await load_style_corpus()
        notes = self.ensure_notes()

        fingerprint = await compute_voice_fingerprint(
            self.snapshot.conversation, corpus_samples, self.target_format
        )
        if (
            self.snapshot.voice_fingerprint == fingerprint
            and self.snapshot.voice_file_paths
        ):
            await self.hooks.on_progress(
                "Voice: inputs unchanged, reusing cached files"
            )
            self.snapshot.stage = "voice"
            await self.save_snapshot()
            await self.update_overview()
            return

        if (
            self.snapshot.voice_fingerprint
            and self.snapshot.voice_fingerprint != fingerprint
        ):
            invalidate_merged_cache()
            logger.info("Voice inputs changed — re-analyzing")

        explicit_prescriptive: list[tuple[str, str]] = []
        analyzable_samples: list[str] = []
        analyzable_sources: list[str] = []
        for sample, source, stype in zip(corpus_samples, corpus_sources, corpus_types):
            if stype == "prescriptive":
                explicit_prescriptive.append((sample, source))
            else:
                analyzable_samples.append(sample)
                analyzable_sources.append(source)

        all_servers = {**self.research_servers, **self.compute_servers}
        all_tool_names = research_tool_names() + self.compute_tool_names

        analyses = await analyze_voice_individually(
            self.snapshot.conversation,
            analyzable_samples,
            corpus_sources=analyzable_sources,
            target_format=self.target_format,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
            mcp_servers=all_servers,
            mcp_tool_names=all_tool_names,
        )
        if not analyses and not explicit_prescriptive:
            raise PipelineError("Voice analysis produced no output")

        voice_file_paths: list[str] = []
        voice_tab_parts: list[str] = []
        n_voice = 0
        n_auto_prescriptive = 0
        presc_idx = 0

        source_by_label: dict[str, str] = {}
        if analyzable_samples:
            labels = analyzable_sources or [
                f"sample-{i}" for i in range(len(analyzable_samples))
            ]
            for sample, label in zip(analyzable_samples, labels):
                source_by_label[label] = sample

        for i, (label, text, is_prescriptive) in enumerate(analyses):
            slug = slugify(label)
            if is_prescriptive and label in source_by_label:
                raw = source_by_label[label]
                envelope = await notes.save_content(
                    f"prescriptive_{presc_idx}_{slug}",
                    raw,
                    stage="voice",
                    content_type="prescriptive",
                    label=label,
                )
                path = Path(envelope.path)
                voice_tab_parts.append(f"## Prescriptive (auto): {label}\n\n{raw}")
                presc_idx += 1
                n_auto_prescriptive += 1
            else:
                envelope = await notes.save_content(
                    f"voice_{n_voice}_{slug}",
                    text,
                    stage="voice",
                    content_type="voice_analysis",
                    label=label,
                )
                path = Path(envelope.path)
                voice_tab_parts.append(f"## {label}\n\n{text}")
                n_voice += 1
            voice_file_paths.append(str(path))

        for sample, source in explicit_prescriptive:
            slug = slugify(source)
            envelope = await notes.save_content(
                f"prescriptive_{presc_idx}_{slug}",
                sample,
                stage="voice",
                content_type="prescriptive",
                label=source,
            )
            voice_file_paths.append(envelope.path)
            voice_tab_parts.append(f"## Prescriptive: {source}\n\n{sample}")
            presc_idx += 1

        for i, (sample, source) in enumerate(
            zip(analyzable_samples, analyzable_sources)
        ):
            if source not in source_by_label:
                continue
            label_entry = next(
                (a for a in analyses if a[0] == source and a[2]),
                None,
            )
            if label_entry:
                continue
            slug = slugify(source)
            envelope = await notes.save_content(
                f"corpus_{i}_{slug}",
                sample,
                stage="voice",
                content_type="source",
                label=source,
            )
            voice_file_paths.append(envelope.path)

        self.snapshot.voice_file_paths = voice_file_paths
        self.snapshot.voice_profile = "individual"
        self.snapshot.voice_fingerprint = fingerprint
        self.snapshot.stage = "voice"
        await self.save_snapshot()

        n_explicit = len(explicit_prescriptive)
        parts: list[str] = []
        if n_voice:
            parts.append(f"{n_voice} analyses")
        if n_auto_prescriptive:
            parts.append(f"{n_auto_prescriptive} auto-prescriptive (verbatim)")
        if n_explicit:
            parts.append(f"{n_explicit} prescriptive (explicit)")
        await self.hooks.on_progress(f"Voice: {' + '.join(parts)} saved")
        async with gdoc_nonfatal("write voice tab"):
            voice_tab_id = self.known_tabs["Voice"]
            voice_tab_content = "\n\n---\n\n".join(voice_tab_parts)
            await do_write_tab(
                self.doc_id, voice_tab_id, voice_tab_content, session_state=self.state
            )
            assert self.tabs is not None
            await self.tabs.record_from_doc(voice_tab_id, "Voice")
        await self.update_overview()

    async def stage_plan(self) -> None:
        await self.hooks.on_stage("plan", "Planning article structure")
        await self.update_overview(active_stage="plan")
        notes = self.ensure_notes()
        plan_path = notes.artifact_path("plan")

        async def sync_plan_tab() -> None:
            text = render_plan_progress(plan_path)
            async with gdoc_nonfatal("sync plan tab"):
                await do_write_tab(
                    self.doc_id,
                    self.known_tabs["Plan"],
                    text,
                    session_state=self.state,
                )

        directions = notes.load_directions()
        if self.snapshot.author_instructions:
            author_intent = (
                f"Author's instructions: {self.snapshot.author_instructions}"
            )
            directions = (
                f"{author_intent}\n\n{directions}" if directions else author_intent
            )

        plan = await plan_article(
            notes,
            target_format=self.target_format,
            voice_file_paths=self.snapshot.voice_file_paths,
            source_file_paths=self.snapshot.source_file_paths,
            author_notes=self.author_notes,
            author_directions=directions,
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
            on_plan_update=sync_plan_tab,
        )
        await self.post_author_notes()
        self.snapshot.plan = plan
        self.snapshot.stage = "plan"
        self.state.title = plan.title
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
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
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
        research_path = notes.artifact_path("research")

        async def sync_research_tab() -> None:
            text = render_research_progress(research_path)
            async with gdoc_nonfatal("sync research tab"):
                await do_write_tab(
                    self.doc_id,
                    self.known_tabs["Research"],
                    text,
                    session_state=self.state,
                )

        research = await research_plan(
            notes,
            feedback_path=feedback_path,
            servers=self.research_servers,
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            author_notes=self.author_notes,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
            on_research_update=sync_research_tab,
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

        refined_path = notes.artifacts_dir / "plan_refined.json"

        async def sync_refine_tab() -> None:
            text = render_plan_progress(refined_path)
            async with gdoc_nonfatal("sync plan tab (refine)"):
                await do_write_tab(
                    self.doc_id,
                    self.known_tabs["Plan"],
                    text,
                    session_state=self.state,
                )

        refined = await refine_plan(
            notes,
            voice_file_paths=self.snapshot.voice_file_paths,
            feedback_path=feedback_path,
            author_notes=self.author_notes,
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
            on_plan_update=sync_refine_tab,
        )
        await self.post_author_notes()
        self.snapshot.plan = refined
        self.snapshot.stage = "refine"
        await self.save_snapshot()

        await self.hooks.on_progress(
            f"Refined plan: {len(refined.sections)} sections (was {initial_count})"
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

        async def write_and_publish(section: SectionPlan, idx: int) -> SectionDraft:
            tid = self.tab_ids.get(section.title, "")
            draft_path = self.get_draft_path(section.title)
            syncer: DraftSyncer | None = None
            if tid:
                syncer = DraftSyncer(
                    self.doc_id,
                    tid,
                    draft_path,
                    session_state=self.state,
                )
                await syncer.start()
            try:
                draft = await write_section(
                    section.title,
                    notes=notes,
                    draft_path=draft_path,
                    voice_file_paths=self.snapshot.voice_file_paths,
                    feedback_path=feedback_path,
                    section_context=self.build_neighbor_context(plan, section.title),
                    target_format=self.effective_format,
                    servers=self.research_servers,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    author_notes=self.author_notes,
                    compute_servers=self.compute_servers,
                    compute_tool_names=self.compute_tool_names,
                    trace_logger=self.trace_logger,
                    cost_accumulator=self.cost_accumulator,
                )
            finally:
                if syncer is not None:
                    await syncer.stop()
            self.snapshot.section_drafts[section.title] = draft

            if (
                tid
                and draft.content
                and not draft.content.startswith("[Section failed")
            ):
                assert self.tabs is not None
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

        if "Draft" not in self.known_tabs:
            self.known_tabs["Draft"] = await do_create_tab(
                self.doc_id, "Draft", session_state=self.state
            )
        self.draft_tab_id = self.known_tabs["Draft"]

        syncer = DraftSyncer(
            self.doc_id,
            self.draft_tab_id,
            output_path,
            tab_name="Draft",
            session_state=self.state,
            interval=8.0,
        )
        await syncer.start()
        try:
            merged = await merge_sections(
                section_paths,
                notes=notes,
                output_path=output_path,
                voice_file_paths=self.snapshot.voice_file_paths,
                feedback_path=feedback_path,
                target_format=self.effective_format,
                author_notes=self.author_notes,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=self.compute_servers,
                compute_tool_names=self.compute_tool_names,
                trace_logger=self.trace_logger,
                heartbeat=merge_heartbeat,
                cost_accumulator=self.cost_accumulator,
            )
        finally:
            await syncer.stop()
        await self.post_author_notes()
        self.snapshot.merged = merged
        self.snapshot.stage = "merge"
        await self.save_snapshot()

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
            notes,
            draft_path,
            servers=self.research_servers,
            voice_file_paths=self.snapshot.voice_file_paths,
            author_notes=self.author_notes,
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()
        consolidated = consolidate_findings(findings)
        findings = consolidated.findings
        self.dropped_suggestions = consolidated.dropped_suggestions
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

        syncer = DraftSyncer(
            self.doc_id,
            self.draft_tab_id,
            output_path,
            tab_name="Draft",
            session_state=self.state,
            interval=8.0,
        )
        await syncer.start()
        try:
            output = await rewrite_final(
                notes,
                draft_path,
                self.snapshot.findings,
                output_path=output_path,
                voice_file_paths=self.snapshot.voice_file_paths,
                feedback_path=feedback_path,
                target_format=self.effective_format,
                dropped_suggestions=getattr(self, "dropped_suggestions", 0),
                author_notes=self.author_notes,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=self.compute_servers,
                compute_tool_names=self.compute_tool_names,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
        finally:
            await syncer.stop()
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

        chosen_format = self.effective_format
        await self.hooks.on_stage("format", f"Applying {chosen_format} formatting")
        await self.update_overview(active_stage="format")
        article_text = output.content or output.summary
        final_content = await apply_format(article_text, plan.title, chosen_format)

        async with gdoc_nonfatal("write final tab"):
            from inkwell.agent.tools.google_docs import write_with_continuation

            tab_ids = await write_with_continuation(
                self.doc_id, "Final", final_content, session_state=self.state
            )
            if tab_ids:
                self.final_tab_id = tab_ids[0]
                self.known_tabs["Final"] = tab_ids[0]

        output.google_doc_id = self.doc_id
        output.google_doc_url = self.doc_url
        output.word_count = len(final_content.split())  # claude: ignore
        self.snapshot.stage = "format"
        self.state.set_stage("complete")
        await self.save_snapshot()
        await self.update_overview()

    # -- Sync / restart logic ----------------------------------------------

    async def sync_if_requested(self) -> None:
        if not self.hooks.sync_requested.is_set():
            return
        self.hooks.sync_requested.clear()
        logger.info("Sync requested — gathering feedback")
        await self.hooks.on_stage("sync", "Polling comments, edits, and terminal input")
        n = await self.gather_feedback()
        if n:
            await self.hooks.on_progress(f"Found {n} feedback item(s)")
        else:
            await self.hooks.on_progress("No pending feedback")
        await self.check_and_maybe_restart()

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
                case PatchAction(
                    section=section, target_text=target, instruction=instr
                ):
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
            f'## Edit Target\n\n"{target_text}"\n\n'
            f"## Instruction\n\n{instruction}\n\n"
            f"Read the draft with the Read tool, then use Edit to apply "
            f"the change. Use Write if the change is too large for Edit."
        )
        all_servers = {
            **self.research_servers,
            **self.source_servers,
            **self.compute_servers,
        }
        all_tools = (
            research_tool_names() + self.source_tool_names + self.compute_tool_names
        )
        await query(
            task,
            model="claude-opus-4-6",
            system_prompt=SECTION_WRITER_PROMPT,
            tools=BUILTIN_WRITE_TOOLS,
            max_thinking_tokens=128_000 - 1,
            permission_mode="bypassPermissions",
            mcp_servers=all_servers,
            allowed_tools=all_tools,
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

        new_questions = [q for _section_plan, qs in rewrite_tasks for q in qs]
        if new_questions:
            await self.hooks.on_progress(
                f"Researching {len(new_questions)} new question(s) before rewriting"
            )
            self.snapshot.research = await research_questions(
                notes,
                new_questions,
                servers=self.research_servers,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=self.compute_servers,
                compute_tool_names=self.compute_tool_names,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )

        feedback_path = await self.prepare_feedback("restart")

        def rewrite_coro(section_plan: SectionPlan):
            return write_section(
                section_plan.title,
                notes=notes,
                draft_path=self.get_draft_path(section_plan.title),
                voice_file_paths=self.snapshot.voice_file_paths,
                feedback_path=feedback_path,
                section_context=self.build_neighbor_context(plan, section_plan.title),
                target_format=self.effective_format,
                servers=self.research_servers,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                author_notes=self.author_notes,
                compute_servers=self.compute_servers,
                compute_tool_names=self.compute_tool_names,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )

        all_plans = [sp for sp, _qs in rewrite_tasks] + add_tasks
        all_coros = [rewrite_coro(sp) for sp in all_plans]

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

    def build_neighbor_context(self, plan: ArticlePlan, title: str) -> str:
        """Describe adjacent sections so a writer connects rather than re-opens."""
        titles = [s.title for s in plan.sections]
        if title not in titles:
            return ""
        idx = titles.index(title)
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

    # -- Standby loop ------------------------------------------

    async def wait_for_revision_or_sync(
        self, state: WritingSessionState
    ) -> tuple[str | None, bool]:
        revision_task = asyncio.ensure_future(self.hooks.collect_revision(state))
        sync_task = asyncio.ensure_future(self.hooks.sync_requested.wait())
        done, pending = await asyncio.wait(
            {revision_task, sync_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if revision_task in done:
            return revision_task.result(), sync_task in done
        self.hooks.sync_requested.clear()
        return None, True

    async def standby_loop(
        self, initial_wait: float = 120.0, quiet_wait: float = 90.0
    ) -> None:
        while True:
            revision, from_sync = await self.wait_for_revision_or_sync(self.state)
            if revision is None and not from_sync:
                continue

            batch = [revision] if revision else []
            if not from_sync:
                try:
                    await asyncio.wait_for(
                        self.drain_feedback_batch(batch, quiet_wait),
                        timeout=initial_wait,
                    )
                except asyncio.TimeoutError:
                    pass

            await self.hooks.on_stage(
                "sync" if from_sync and not batch else "revise",
                "Syncing feedback"
                if from_sync and not batch
                else f"Revising: {' | '.join(batch)[:60]}",
            )
            self.state.set_stage("revising")

            self.snapshot.stage = "revise"
            await self.update_overview(active_stage="revise")

            n = await self.gather_feedback()
            if n:
                await self.hooks.on_progress(f"Found {n} feedback item(s)")
            for revision_text in batch:
                await self.classify_and_record_terminal(revision_text)

            if not n and not batch:
                await self.hooks.on_progress("Nothing to revise")
                continue

            plan = self.snapshot.plan
            if plan is None:
                break

            notes = self.ensure_notes()
            has_plan_breaking = await notes.has_plan_breaking()
            if has_plan_breaking and self.restart_count < self.max_restarts:
                await self.check_and_maybe_restart()
                await notes.clear_plan_breaking()

            await self.do_standby_rewrite()

    async def do_standby_rewrite(self) -> None:
        plan = self.snapshot.plan
        if plan is None:
            return

        notes = self.ensure_notes()

        current_content = ""
        if self.final_tab_id:
            try:
                current_content = await do_read_tab(self.doc_id, self.final_tab_id)
            except (RuntimeError, OSError):
                logger.warning("Could not read Final tab; using last known")

        if not current_content and self.snapshot.output:
            current_content = self.snapshot.output.content
        if not current_content:
            logger.warning("No current article content — skipping standby rewrite")
            return

        # Write current content to a temp draft for the rewriter to read
        standby_draft = self.get_draft_path("standby-input")
        standby_draft.write_text(current_content, encoding="utf-8")
        standby_output = self.get_draft_path("final-standby")

        feedback_path = await notes.render_feedback_file("standby")

        # Original review findings were already applied in stage_rewrite;
        # re-annotating them here would anchor stale excerpts onto revised
        # text. Standby rewrites are driven by author feedback alone.
        output = await rewrite_final(
            notes,
            standby_draft,
            [],
            output_path=standby_output,
            voice_file_paths=self.snapshot.voice_file_paths,
            feedback_path=feedback_path,
            target_format=self.effective_format,
            author_notes=self.author_notes,
            source_servers=self.source_servers,
            source_tool_names_list=self.source_tool_names,
            compute_servers=self.compute_servers,
            compute_tool_names=self.compute_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        await self.post_author_notes()

        final_content = await apply_format(
            output.content, plan.title, self.effective_format
        )
        async with gdoc_nonfatal("write standby rewrite"):
            if self.final_tab_id:
                await do_write_tab(
                    self.doc_id,
                    self.final_tab_id,
                    final_content,
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
            if self.hooks.sync_requested.is_set():
                return
            revision_task = asyncio.ensure_future(
                self.hooks.collect_revision(self.state)
            )
            sync_task = asyncio.ensure_future(self.hooks.sync_requested.wait())
            done, pending = await asyncio.wait(
                {revision_task, sync_task},
                timeout=quiet_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if sync_task in done:
                return
            if revision_task not in done:
                return
            extra = revision_task.result()
            if extra is None:
                return
            batch.append(extra)

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
                        self.doc_id,
                        tab_name,
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
                self.doc_id,
                sections_parent_id,
                f"# Sections ({len(plan.sections)})\n\n{sections_summary}",
                session_state=self.state,
            )

    async def write_research_tab(self, research: ResearchCompilation) -> None:
        async with gdoc_nonfatal("write research tab"):
            research_text = (
                f"# Research Findings\n\n{len(research.findings)} findings\n"
            )
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
            from inkwell.agent.tools.google_docs import write_with_continuation

            tab_ids = await write_with_continuation(
                self.doc_id, "Research", research_text, session_state=self.state
            )
            assert self.tabs is not None
            if tab_ids:
                await self.tabs.record_from_doc(tab_ids[0], "Research")

    async def write_review_tab(self, findings: list[ReviewFinding]) -> None:
        async with gdoc_nonfatal("write review tab"):
            if "Review" not in self.known_tabs:
                self.known_tabs["Review"] = await do_create_tab(
                    self.doc_id, "Review", session_state=self.state
                )

            review_text = f"# Review Findings\n\n{len(findings)} total findings\n"
            critical_specs: list[CommentSpec] = []
            for finding in findings:
                review_text += (
                    f"\n## [{finding.reviewer}] {finding.location}\n\n"
                    f"**Severity:** {finding.severity}\n\n"
                    f"{finding.issue}\n\n"
                    f"**Suggestion:** {finding.suggestion}\n"
                )
                if finding.severity == "critical":
                    critical_specs.append(
                        CommentSpec(
                            content=(
                                f"[{finding.reviewer.upper()}] {finding.issue}\n\n"
                                f"Suggestion: {finding.suggestion}"
                            ),
                            anchor_text=finding.text_excerpt or None,
                        )
                    )

            if critical_specs:
                async with gdoc_nonfatal("post review comments"):
                    await do_insert_comments_batch(
                        self.doc_id,
                        critical_specs,
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
                "extract",
                "voice",
                "plan",
                "research",
                "assumptions",
                "refine",
                "write",
                "merge",
                "review",
                "rewrite",
                "format",
            ]
            completed_idx = (
                all_stages.index(completed_stage)
                if completed_stage in all_stages
                else -1
            )
            active_idx = (
                all_stages.index(active_stage)
                if active_stage and active_stage in all_stages
                else -1
            )
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
                parts.append(
                    f"**Research:** {len(plan.research_questions)} questions queued\n"
                )

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
                suggestions = sum(
                    1 for f in snap.findings if f.severity == "suggestion"
                )
                parts.append(
                    f"\n## Review\n\n{critical} critical, {suggestions} suggestions"
                )

            if self.state.pending_questions:
                n = len(self.state.pending_questions)
                parts.append(f"\n**Questions for author:** {n} pending")

            if active_stage == "revise":
                parts.append("\n*Processing your feedback...*")
            else:
                parts.append("\n*Comment on any tab to give feedback.*")

            await do_write_tab(
                self.doc_id,
                self.overview_tab_id,
                "\n".join(parts),
                session_state=self.state,
            )


async def run_pipeline(
    *,
    sources: list[str],
    refs: list[str] | None = None,
    target_format: str = "auto",
    existing_doc_id: str | None = None,
    session_state: WritingSessionState | None = None,
    notes: PipelineNotes | None = None,
    trace_logger: TraceLogger | None = None,
    listener: PipelineListener | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Run the complete writing pipeline."""
    runner = PipelineRunner(
        sources=sources,
        refs=refs,
        target_format=target_format,
        existing_doc_id=existing_doc_id,
        session_state=session_state,
        notes=notes,
        trace_logger=trace_logger,
        listener=listener,
        cost_accumulator=cost_accumulator,
    )
    return await runner.run()
