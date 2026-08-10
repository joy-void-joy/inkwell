"""Unified writing pipeline with restartable state machine.

Stages read inputs from files (via built-in Read) and produce output
via incremental MCP tools (plan, research, review) or built-in Write/Edit
(prose stages). This keeps context lean — agents pull what they need
rather than receiving everything in the prompt.
"""

import asyncio
import difflib
import logging
import shutil
import tempfile
import unicodedata
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
)
from contextlib import asynccontextmanager
from pathlib import Path


from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.client import (
    HeartbeatCallback,
    SessionPolicy,
    active_block_callback,
    is_interrupt,
    query,
    result_text,
    session_policy,
)
from lup.mcp import LupMcpTool, McpServerEntry, create_mcp_server
from lup.types import StringMap
from lup.runtime.models import AnyTurnBlock
from lup.runtime.usage import CostAccumulator
from lup.workspace.paths import find_project_root
from lup.sandbox.container import Sandbox
from lup.telemetry.blocks import extract_block_info
from lup.telemetry.trace import TraceLogger

from inkwell.agent.config import current_settings, load_settings, stage_model
from inkwell.agent.models import (
    ArticlePlan,
    AssumptionsList,
    AssumptionTag,
    AuthorNote,
    ClassifiedComment,
    MergedDraft,
    PipelineSnapshot,
    ResearchCompilation,
    RestartQueue,
    RestartStrategy,
    ReviewFinding,
    ReviewOutput,
    SectionDraft,
    SectionPlan,
    WritingOutput,
)
from inkwell.agent.content import ContentManifest
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.watcher import (
    ACKNOWLEDGE_TEMPLATES,
    PollingWatcher,
    create_comment_watcher,
    create_source_watcher,
)
from inkwell.agent.session import AuthorComment, CommentLedger, WritingSessionState
from inkwell.agent.stages import (
    ASSUMPTIONS_PROMPT,
    COMMENT_CLASSIFIER_PROMPT,
    COVERAGE_REVIEWER_PROMPT,
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
    ORCHESTRATOR_PROMPT,
    PLANNER_SYSTEM,
    MERGE_PROMPT,
    REFINER_SYSTEM,
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    RESOLVER_PROMPT,
    SECTION_WRITER_PROMPT,
    SINGLE_WRITER_PROMPT,
    SOURCE_FIDELITY_REVIEWER_PROMPT,
    STYLE_REVIEWER_PROMPT,
    get_format_guidance,
)
from inkwell.agent.extract_agent import assemble_sources, run_extraction_agent
from inkwell.agent.tool_policy import research_tool_names, review_tool_names
from inkwell.agent.tools.extract import (
    EXTRACT_TOOLS as EXTRACT_MCP_TOOLS,
    is_gdoc_url,
    do_extract_conversation,
    do_extract_file,
    do_extract_gdoc,
    do_extract_lesswrong,
    extract_source_tab_only,
    fetch_gdoc_comments,
    parse_gdoc_id,
)
from inkwell.agent.sandbox_image import (
    INKWELL_SANDBOX_IMAGE,
    ensure_sandbox_image,
    sandbox_image_available,
)
from inkwell.agent.tools.research.fetch import do_fetch_source
from inkwell.agent.tools.source_consult import (
    build_reading_notes,
    build_source_registry_async,
    load_source_registry,
    make_source_consult_tools,
    reading_notes_dir,
    registry_path_for,
)
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
    PlanCollector,
    PlanFile,
    ResearchCollector,
    ReviewCollector,
    make_assumptions_tools,
    make_glossary_tools,
    make_note_tool,
    make_plan_tools,
    make_research_output_tools,
    make_review_output_tools,
    seed_glossary,
)
from inkwell.agent.tools.query_artifacts import make_query_tools
from inkwell.agent.tools.voice import (
    StyleSample,
    analyze_voice_individually,
    compute_voice_fingerprint,
    invalidate_merged_cache,
    load_style_corpus,
)

logger = logging.getLogger(__name__)

BUILTIN_READ_TOOLS = ["Read", "Grep", "Glob", "Agent"]
BUILTIN_WRITE_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob", "Agent"]

DISPLAY_STAGES = [
    "extract",
    "voice",
    "plan",
    "research",
    "assumptions",
    "refine",
    "write",
    "merge",
    "review",
    "resolve",
    "rewrite",
    "format",
]
"""Ordered author-facing pipeline backbone, shared by the resume loop, the
Overview tab, and the terminal stage counter so all three agree on which
stage is which and how many there are. ``preprocess`` is an internal
pre-stage and is prepended where the runner needs it."""

CHECKPOINT_STAGES = [s for s in DISPLAY_STAGES if s != "resolve"]
"""Backbone stages that persist a resumable snapshot — the valid targets for
``resume --from`` and for a ``--stop-after`` pause. ``resolve`` runs between
review and rewrite without checkpointing, so it is neither a resume boundary
nor a place a run can be paused and continued from."""


def validate_stop_after(stage: str | None) -> str | None:
    """Normalize and validate a requested stop point, or raise ValueError.

    ``None``/empty means "run to the end" and passes through. Every other
    value must name a checkpoint stage, so a typo fails loudly at construction
    rather than silently never firing and letting the run finish unpaused.
    """
    if stage is None or not stage.strip():
        return None
    normalized = stage.strip().lower()
    if normalized not in CHECKPOINT_STAGES:
        raise ValueError(
            f"Invalid stop point '{stage}'. Valid stages: "
            f"{', '.join(CHECKPOINT_STAGES)}"
        )
    return normalized


def summarize_inputs(inputs: list[str]) -> str:
    """A compact, log-safe preview of the inputs a run received.

    Labels each input a file, URL, or freeform text with a short preview, so a
    dropped attachment — only the author's directions arrived — is obvious in
    the log rather than hidden behind a bare "1 source(s)" count.
    """

    def describe(value: str) -> str:
        stripped = value.strip()
        if Path(stripped).is_file():
            return f"[file] {Path(stripped).name}"
        if stripped.startswith(("http://", "https://")):
            return f"[url] {stripped[:60]}"
        return f"[text {len(stripped)}ch] {stripped[:50]!r}"

    parts = [describe(value) for value in inputs]
    return "; ".join(parts) if parts else "(none)"


REACTIVE_LABELS = ("classify", "orchestrator")
"""Trace-label prefixes for one-shot reactive helpers (comment classification,
restart orchestration): persisted but never resumed, since each invocation is a
fresh decision rather than a continuation of a prior conversation."""


def is_resumable_label(label: str) -> bool:
    """Whether a stage label denotes a resumable pipeline agent.

    Reactive helpers fire fresh each time and would only carry stale context if
    resumed; the linear pipeline stages and section writers are the ones a
    resume should continue.
    """
    return not label.startswith(REACTIVE_LABELS)


def relocate_transcripts(
    src_config: str | None,
    dst_config: str | None,
    session_ids: StringMap,
) -> StringMap:
    """Mirror recorded session transcripts into the resuming profile's dir.

    A transcript is filed under the creating profile's ``CLAUDE_CONFIG_DIR``;
    resuming under a different profile only finds it once the ``.jsonl`` is
    copied into that profile's ``projects`` tree. The cwd is pinned, so the
    relative path is identical and a plain copy lands where the resuming SDK
    looks. Returns the ids whose transcript was found and placed — the only
    ones safe to resume; the rest fall back to a fresh session.
    """
    if not src_config or not dst_config:
        return {}
    if src_config == dst_config:
        return dict(session_ids)
    src, dst = Path(src_config), Path(dst_config)
    label_for = {sid: label for label, sid in session_ids.items()}

    def placed() -> Iterator[str]:
        """The labels whose transcript was found under `src` and copied across."""
        for transcript in (src / "projects").glob("**/*.jsonl"):
            if transcript.stem not in label_for:
                continue
            label = label_for[transcript.stem]
            target = dst / transcript.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(transcript, target)
            yield label

    return {label: session_ids[label] for label in placed()}


class PipelineError(Exception):
    """Raised when a pipeline stage fails to produce valid output."""


class PipelineInterrupted(PipelineError):
    """Raised when an interrupt (SIGINT) cut the run short before any output.

    Distinct from a failure: the per-stage snapshots are intact, so the run is
    resumable. ``stage`` names where it stopped, so callers can report *why*
    no output exists instead of the misleading "completed without output".
    """

    def __init__(self, stage: str | None) -> None:
        self.stage = stage
        where = stage or "an early stage"
        super().__init__(
            f"Pipeline interrupted during {where} before producing output — "
            "resume to continue"
        )


class PipelineStopRequested(Exception):
    """Signals a clean halt at a configured stop point, carrying the stage name.

    Raised at a stage boundary when the run was launched with ``stop_after``
    set to the stage that just finished. It unwinds out of the stage sequence
    so the runner can finalize a pause instead of continuing — the stage's
    snapshot is already saved, so a later resume picks up from the next stage.
    """

    def __init__(self, stage: str, message: str = "") -> None:
        super().__init__(stage)
        self.stage = stage
        self.message = message


class TabEdit(BaseModel):
    """A detected author edit on a GDoc tab — diff only, not full content."""

    tab: str = Field(description="Tab label (e.g. 'Source', 'Section: Introduction')")
    diff: str = Field(description="Changed regions with surrounding context lines")
    original_snippet: str = Field(
        default="",
        description="Original text from the changed region, for revert suggestions",
    )


class SectionTab(BaseModel):
    """A planned section paired with the GDoc tab that holds its draft."""

    title: str = Field(description="Section title, as the plan names it")
    tab_id: str = Field(description="Tab the section's draft is written to")


GDOC_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "—": "--",
        "–": "-",
        " ": " ",
    }
)
"""Smart punctuation a GDoc substitutes as the author types, mapped back to the
ASCII the pipeline compares against."""


def normalize_gdoc_text(text: str) -> str:
    """Normalize text for comparison, stripping GDoc formatting artifacts."""
    collapsed = " ".join(unicodedata.normalize("NFC", text).split())
    return collapsed.translate(GDOC_CHAR_MAP)


def extract_edit_summary(original: str, current: str, context_lines: int = 2) -> str:
    """Extract a concise summary of what changed between two texts."""
    orig_lines = original.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(None, orig_lines, curr_lines)

    def rendered(tag: str, i1: int, i2: int, j1: int, j2: int) -> Iterator[str]:
        """One changed region: the lines before it, the change, the lines after."""
        ctx_before = orig_lines[max(0, i1 - context_lines) : i1]
        if ctx_before:
            yield "  " + "\n  ".join(ctx_before)

        match tag:
            case "replace":
                yield "- " + "\n- ".join(orig_lines[i1:i2])
                yield "+ " + "\n+ ".join(curr_lines[j1:j2])
            case "delete":
                yield "- " + "\n- ".join(orig_lines[i1:i2])
            case "insert":
                yield "+ " + "\n+ ".join(curr_lines[j1:j2])

        ctx_after = orig_lines[i2 : i2 + context_lines]
        if ctx_after:
            yield "  " + "\n  ".join(ctx_after)

    blocks = [
        "\n".join(rendered(tag, i1, i2, j1, j2))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]
    return "\n---\n".join(blocks)


def extract_deleted_text(original: str, current: str) -> str:
    """Return the original text from deleted/replaced regions, for revert quoting."""
    orig_lines = original.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(None, orig_lines, curr_lines)
    deleted = [
        line
        for tag, i1, i2, _j1, _j2 in matcher.get_opcodes()
        if tag in ("delete", "replace")
        for line in orig_lines[i1:i2]
    ]
    return "\n".join(deleted)


# ---------------------------------------------------------------------------
# Slugify + research splitting
# ---------------------------------------------------------------------------


SLUG_CHAR_MAP = str.maketrans({**dict.fromkeys("/:.,;!?\"'()[]{}"), "-": " "})
"""Punctuation a slug drops outright, plus the hyphen, which becomes a separator
so that runs of them collapse with the surrounding whitespace."""

SLUG_MAX_CHARS = 80


def slugify(label: str) -> str:
    """Turn a section title or label into a filesystem-safe slug."""
    return "-".join(label.lower().translate(SLUG_CHAR_MAP).split())[:SLUG_MAX_CHARS]


ASSUMPTION_TAG_PREFIX: dict[AssumptionTag, str] = {
    "direction_check": "[DIRECTION]",
    "assumption": "[ASSUMPTION]",
    "question": "[QUESTION]",
    "confusion": "[UNCLEAR]",
}
"""How each surfaced uncertainty announces itself in the comment it posts."""


AUTHOR_MARKERS = ("TODO", "FIXME", "BOTEC", "XXX")

MARKDOWN = MarkdownIt()
"""Reads a prose line's text content, so the block markers an author typed
around a marker — heading, bullet, quote, ordered item — are the parser's
business rather than a prefix this module strips by hand."""


def extract_author_markers(text: str) -> list[str]:
    """Pull inline author markers (TODO/FIXME/BOTEC/...) out of source text.

    Authors leave these in drafts to flag unfinished work — a number to fill,
    an estimate to run, a passage to rewrite. They must surface as explicit
    tasks; otherwise the pipeline treats the surrounding prose as finished and
    the flagged work silently disappears.
    """

    def marked() -> Iterator[str]:
        for raw in text.splitlines():
            for token in MARKDOWN.parse(raw):
                if token.type != "inline":
                    continue
                upper = token.content.upper()
                if any(marker in upper for marker in AUTHOR_MARKERS):
                    yield token.content

    return list(dict.fromkeys(marked()))


def add_source_refs(manifest: ContentManifest, notes: PipelineNotes) -> None:
    """List the session's authoritative source documents in a stage manifest."""
    for doc in load_source_registry(registry_path_for(notes.artifacts_dir)):
        pages = f", {doc.page_count} pages" if doc.page_count else ""
        manifest.add(
            doc.path,
            "source",
            doc.label,
            instruction=(
                f"authoritative source document ({doc.kind}{pages}) — verify "
                "claims against it via consult_source/find_in_source or Read"
            ),
        )
    for notes_file in sorted(reading_notes_dir(notes.artifacts_dir).glob("*.md")):
        manifest.add(
            notes_file,
            "source",
            notes_file.stem,
            instruction=(
                "reading notes: verbatim definitions and statements with page "
                "refs — cheaper than re-reading the document"
            ),
        )


def render_source_lines(notes: PipelineNotes) -> str:
    """Render source-document references for stages that use plain file refs."""
    docs = load_source_registry(registry_path_for(notes.artifacts_dir))
    if not docs:
        return ""
    lines = [
        "Authoritative source documents (verify claims against these via "
        "consult_source/find_in_source or Read):"
    ]
    for doc in docs:
        pages = f" ({doc.page_count} pages)" if doc.page_count else ""
        lines.append(f"- {doc.label}: {doc.path}{pages}")
    return "\n".join(lines) + "\n"


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


def directions_block(notes: PipelineNotes) -> str:
    """Render the source-document author comments for a stage task, or ''.

    The planner distills these into plan.author_direction, but the raw
    comments — and the author's replies, which outrank the original comment —
    carry nuance the distillation drops. Stages that decide structure or
    final wording read them directly so an author instruction can't go
    invisible after planning.
    """
    directions = notes.load_directions()
    if not directions:
        return ""
    return (
        "The author left comments on the source document. Treat each as a "
        "direction; where the author replied to a comment, the reply outranks "
        "it. Never cut what the author asked to keep:\n\n"
        f"{directions}\n\n"
    )


def brief_block(notes: PipelineNotes) -> str:
    """Render the author's brief — instructions and requested outputs — or ''.

    The brief is the contract. The planner distills it into the plan, but the
    distillation can soften or drop an imperative, so every deciding stage reads
    the brief directly: a writer choosing how to handle the source, a reviewer
    judging the draft, the rewriter finalizing.
    """
    brief = notes.load_brief()
    if not brief:
        return ""
    return (
        "The author's brief — their own instructions and the outputs they "
        "asked for. This is the contract: honor it over any genre, format, or "
        "convention default. Where the brief and a default disagree, the brief "
        "wins:\n\n"
        f"{brief}\n\n"
    )


def author_context_block(notes: PipelineNotes) -> str:
    """The author's brief plus source-document directions, for a stage task.

    Every stage that decides content or wording reads this so an author
    instruction can never go invisible after planning.
    """
    return brief_block(notes) + directions_block(notes)


def academic_assembly_block(target_format: str, output_path: Path) -> str:
    """Document-ownership instructions for the academic merge/rewrite stages.

    The section-writers emit body fragments; whoever assembles them owns the
    whole paper, preamble included. This stage writes a complete, self-contained
    LaTeX document and verifies it compiles, so the preamble declares exactly
    what the body uses rather than a fixed guess made before the body existed.
    """
    if target_format != "academic":
        return ""
    return (
        f"This stage owns the complete LaTeX document at {output_path}. Produce "
        f"a self-contained paper: a preamble (\\documentclass, every "
        f"\\usepackage the body needs, and a \\newtheorem for each theorem-style "
        f"environment the body uses — remark, notation, example, claim, and the "
        f"like), then \\begin{{document}} ... \\end{{document}} around the "
        f"assembled sections. Then call compile_latex on that file: if it did "
        f"not compile, the log names the exact failure — fix the preamble or "
        f"body and compile again, until it builds. Never hand off a paper that "
        f"does not compile.\n\n"
    )


def render_brief(instructions: str, deliverables: list[str]) -> str:
    """Format the author's instructions and deliverables as a brief block."""
    parts: list[str] = []
    if instructions.strip():
        parts.append(instructions.strip())
    if deliverables:
        items = "\n".join(f"- {d}" for d in deliverables)
        parts.append(f"Requested deliverables:\n{items}")
    return "\n\n".join(parts)


def resolve_writer_mode(mode: str) -> str:
    """Resolve writer_mode='auto' to a concrete mode.

    'parallel' writes each section independently and assembles them with a
    voice-safe reconcile pass; the shared glossary keeps terminology aligned
    and the reconcile pass never rewrites the author's register, so it suits
    every format. 'single' drafts the whole piece in one context, with no
    reconcile pass. 'auto' resolves to parallel.
    """
    if mode != "auto":
        return mode
    return "parallel"


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

    async def on_pause(self, stage: str, doc_url: str) -> None:
        """The run reached its configured stop point and is pausing here.

        Stages through ``stage`` are done and checkpointed; the environment
        tells the author how to review and continue (the Doc is at
        ``doc_url``; a resume picks up from the next stage).
        """
        logger.info("Pipeline paused after stage '%s' (%s)", stage, doc_url)

    async def on_block(self, _block_type: str, _content: str, _prefix: str) -> None:
        pass

    async def on_message(self, source: str, message: str) -> None:
        logger.info("[%s] %s", source, message)

    async def on_state_change(self) -> None:
        """Session state changed mid-stage (e.g. a section finished drafting).

        Stage boundaries already refresh listeners; this fires for the
        finer-grained transitions inside a stage so a live UI can reflect
        per-section progress without waiting for the next stage.
        """

    async def collect_author_input(self, state: WritingSessionState) -> list[str]:
        """Return new author directions typed in the environment."""
        return []

    async def collect_revision(self, state: WritingSessionState) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Tab edit detection
# ---------------------------------------------------------------------------


class WrittenTab(BaseModel):
    """What this run last wrote into one tab, and what it called it."""

    label: str
    content: str


class TabTracker:
    """Tracks written tab content and detects author edits/suggestions."""

    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.written: dict[str, WrittenTab] = {}

    def record(self, tab_id: str, label: str, content: str) -> None:
        self.written[tab_id] = WrittenTab(label=label, content=content)

    async def record_from_doc(self, tab_id: str, label: str) -> None:
        actual = await do_read_tab(self.doc_id, tab_id, as_markdown=True)
        self.record(tab_id, label, actual)

    async def detect_edits(self) -> list[TabEdit]:
        async def edited() -> AsyncGenerator[TabEdit]:
            """Each tab the author has touched since this run last wrote it."""
            for tab_id, written in list(self.written.items()):
                try:
                    current = await do_read_tab(
                        self.doc_id, tab_id, accept_suggestions=True, as_markdown=True
                    )
                except (RuntimeError, OSError):
                    continue
                if not self.has_meaningful_diff(written.content, current):
                    continue
                yield TabEdit(
                    tab=written.label,
                    diff=extract_edit_summary(written.content, current),
                    original_snippet=extract_deleted_text(written.content, current),
                )
                self.record(tab_id, written.label, current)

        return [edit async for edit in edited()]

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
    plan = PlanFile.model_validate_json(plan_path.read_text(encoding="utf-8"))

    def lines() -> Iterator[str]:
        yield f"# {plan.title or 'Planning...'}\n"
        if plan.thesis:
            yield f"**Thesis:** {plan.thesis}\n"
        if plan.author_direction:
            yield f"**Author direction:** {plan.author_direction}\n"
        if plan.sections:
            yield "\n## Sections\n"
            for section in plan.sections:
                yield f"### {section.title or '???'}"
                yield f"{section.summary}\n"
                if section.key_points:
                    yield "Key points: " + ", ".join(section.key_points)
        if plan.research_questions:
            yield "\n## Research Questions\n"
            for question in plan.research_questions:
                yield (
                    f"- [{question.priority}] {question.question} "
                    f"(for: {question.section})"
                )
        if plan.source_quotes:
            yield "\n## Source Quotes\n"
            for quote in plan.source_quotes:
                yield f'- "{quote.text}" — {quote.speaker}'

    return "\n".join(lines())


def render_research_progress(research_path: Path) -> str:
    """Render the current research JSON as markdown for the GDoc Research tab."""
    if not research_path.exists():
        return "# Researching...\n"
    research = ResearchCompilation.model_validate_json(
        research_path.read_text(encoding="utf-8")
    )

    def lines() -> Iterator[str]:
        yield f"# Research Findings\n\n{len(research.findings)} findings\n"
        for finding in research.findings:
            yield f"\n## {finding.question or '???'}\n"
            yield f"{finding.answer}\n"
            yield f"**Confidence:** {finding.confidence}"
            urls = ", ".join(source.url for source in finding.sources)
            if urls:
                yield f"\n**Sources:** {urls}"

    return "\n".join(lines())


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


class ToolServers(BaseModel):
    """MCP servers a stage is given, and the tool names they expose.

    The two travel together everywhere: a session needs the servers to reach
    the tools and the names to allow them, and a stage that merged one without
    the other would be handed tools it may not call.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    servers: dict[str, McpServerEntry]
    tool_names: list[str]

    def merged(self, *others: "ToolServers") -> "ToolServers":
        """This set of servers and tool names, plus every other, as one."""
        groups = [self, *others]
        return ToolServers(
            servers={
                name: server
                for group in groups
                for name, server in group.servers.items()
            },
            tool_names=[name for group in groups for name in group.tool_names],
        )


def build_source_server(
    registry_provider: Callable[[], Path] | None = None,
) -> ToolServers:
    """MCP server for source fetching/extraction — available to ALL pipeline stages.

    With ``registry_provider``, also exposes the source-document tools
    (find_in_source, consult_source, list_source_documents) bound to the
    session's source registry.
    """
    tools = [*FETCH_TOOLS, *EXTRACT_MCP_TOOLS]
    if registry_provider is not None:
        tools.extend(make_source_consult_tools(registry_provider))
    return ToolServers(
        servers={"source": create_mcp_server(name="source", tools=tools)},
        tool_names=[f"mcp__source__{t.name}" for t in tools],
    )


def build_compute_server(
    sandbox: Sandbox,
    artifacts_dir: Path,
    *,
    include_latex: bool = False,
) -> ToolServers:
    """MCP server with code execution (sandbox) and artifact query tools.

    With ``include_latex``, also exposes ``compile_latex`` so a document-owning
    stage can verify and repair its assembled paper against the compiler.
    """
    from inkwell.agent.tools.citations import CITATION_TOOLS
    from inkwell.agent.tools.latex import make_latex_tools

    tools = [
        *sandbox.create_tools(
            usage_notes=(
                "Pipeline session data is mounted read-only at /notes: the plan at "
                "/notes/artifacts/plan.json, research at /notes/artifacts/research.json, "
                "section drafts under /notes/drafts/, and the original source "
                "documents under /notes/artifacts/sources/. Read these instead of "
                "retyping values from memory."
            )
        ),
        *make_query_tools(artifacts_dir),
        *CITATION_TOOLS,
        *(make_latex_tools(sandbox) if include_latex else []),
    ]
    return ToolServers(
        servers={"compute": create_mcp_server(name="compute", tools=tools)},
        tool_names=[f"mcp__compute__{t.name}" for t in tools],
    )


class StageCompute(BaseModel):
    """Per-stage compute handle: the compute MCP server, its tool names, the
    dedicated read-write path the stage writes its deliverable to, and the
    live sandbox (None when Docker is unavailable) for direct shell use."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    servers: dict[str, McpServerEntry]
    tool_names: list[str]
    output_path: Path
    sandbox: Sandbox | None


def build_research_servers() -> dict[str, McpServerEntry]:
    """MCP servers for research-capable stages."""
    return {
        "research": create_mcp_server(name="research", tools=build_research_tools())
    }


def build_output_server(server_name: str, tools: list[LupMcpTool]) -> ToolServers:
    """MCP server + allowed tool names for stage output tools."""
    return ToolServers(
        servers={server_name: create_mcp_server(name=server_name, tools=tools)},
        tool_names=[f"mcp__{server_name}__{t.name}" for t in tools],
    )


def build_note_server(stage: str, notes_collector: list[AuthorNote]) -> ToolServers:
    """Build an MCP server containing just the note_for_author tool."""
    return build_output_server("notes", [make_note_tool(notes_collector, stage)])


def build_glossary_server(glossary_path: Path) -> ToolServers:
    """Build an MCP server with the shared glossary tools bound to a file."""
    return build_output_server("glossary", make_glossary_tools(glossary_path))


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

    def inline() -> Iterator[ReviewFinding]:
        """Splice each finding in after the excerpt it quotes, yielding the ones
        whose excerpt was still there to anchor against."""
        nonlocal annotated
        for f in anchorable:
            if f.text_excerpt not in annotated:
                continue
            if f.severity == "praise":
                annotation = f"\n[PRESERVE:{f.reviewer}] {f.issue}\n"
            else:
                annotation = (
                    f"\n[FINDING:{f.severity}:{f.reviewer}] {f.issue}\n"
                    f"  → {f.suggestion}\n"
                )
            after = annotated.index(f.text_excerpt) + len(f.text_excerpt)
            annotated = f"{annotated[:after]}{annotation}{annotated[after:]}"
            yield f

    anchored = {id(f) for f in inline()}
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

    if is_gdoc_url(url):
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


async def plan_article(
    notes: PipelineNotes,
    *,
    target_format: str = "auto",
    voice_file_paths: list[str] | None = None,
    source_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    author_directions: str = "",
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    on_plan_update: Callable[[], Awaitable[None]] | None = None,
) -> ArticlePlan:
    """Stage 1: Extract a structured article plan from source material."""
    plan_path = notes.artifact_path("plan")
    collector = PlanCollector(plan_path, on_save=on_plan_update)
    note_collector = author_notes if author_notes is not None else []
    stage_tools = build_output_server("output", make_plan_tools(collector)).merged(
        build_note_server("plan", note_collector)
    )
    output_servers = stage_tools.servers
    output_tool_names = stage_tools.tool_names

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
    add_source_refs(manifest, notes)
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
        model=stage_model("plan"),
        system_prompt=PLANNER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    on_plan_update: Callable[[], Awaitable[None]] | None = None,
) -> ArticlePlan:
    """Refine the initial plan using research findings."""
    plan_path = notes.artifact_path("plan")
    refined_path = notes.artifacts_dir / "plan_refined.json"
    collector = PlanCollector(refined_path, on_save=on_plan_update)
    outputs = build_output_server("output", make_plan_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("refine", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
        model=stage_model("refine"),
        system_prompt=REFINER_SYSTEM,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
    servers: dict[str, McpServerEntry] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
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
    outputs = build_output_server("output", make_research_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("research", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
        model=stage_model("research"),
        system_prompt=RESEARCHER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
    servers: dict[str, McpServerEntry] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
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
        collector.research = ResearchCompilation.model_validate_json(
            research_path.read_text(encoding="utf-8")
        )

    outputs = build_output_server("output", make_research_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
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
        model=stage_model("research"),
        system_prompt=RESEARCHER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
    servers: dict[str, McpServerEntry] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    glossary_servers: dict[str, McpServerEntry] | None = None,
    glossary_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> SectionDraft:
    """Write a single section using built-in Write. Called in parallel."""
    if servers is None:
        servers = build_research_servers()

    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server(f"write:{section_title}", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
    all_servers = {
        **servers,
        **note_servers,
        **(source_servers or {}),
        **(compute_servers or {}),
        **(glossary_servers or {}),
    }
    all_tools = (
        research_tool_names()
        + note_tool_names
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
        + (glossary_tool_names or [])
    )

    plan_path = notes.artifact_path("plan")

    manifest = ContentManifest()
    manifest.add(plan_path, "plan", "Article plan")
    add_source_refs(manifest, notes)
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = (
        f'Write the section "{section_title}" for the article.\n\n'
        f"{manifest.render()}\n"
        f"Use list_research to browse all research findings, then read_finding for details.\n\n"
    )
    task += author_context_block(notes)
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

    try:
        await query(
            task,
            model=stage_model("write"),
            system_prompt=SECTION_WRITER_PROMPT,
            tools=BUILTIN_WRITE_TOOLS,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
            mcp_servers=all_servers,
            allowed_tools=all_tools,
            trace_logger=trace_logger,
            prefix=f"[write:{section_title}] ",
            cost_accumulator=cost_accumulator,
        )
    except (
        Exception
    ) as exc:  # claude: ignore — SDK raises generic Exception for usage limits
        # A usage or rate limit can land on the writer's final turn, after the
        # section was already written to disk. The file is the real output, so
        # only a genuine interrupt or an empty/absent draft counts as failure.
        if is_interrupt(exc) or not (
            draft_path.exists() and draft_path.read_text(encoding="utf-8").strip()
        ):
            raise
        logger.warning(
            "Section writer for '%s' raised after producing its draft; "
            "keeping the on-disk content: %s",
            section_title,
            exc,
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


async def write_full_draft(
    *,
    notes: PipelineNotes,
    output_path: Path,
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    target_format: str = "auto",
    servers: dict[str, McpServerEntry] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    heartbeat: HeartbeatCallback | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> str:
    """Write the whole piece with one writer (writer_mode='single').

    One context drafts every section in order, so notation, terminology,
    and framing stay consistent by construction — there is no merge stage.
    """
    if servers is None:
        servers = build_research_servers()

    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("write", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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

    manifest = ContentManifest()
    manifest.add(notes.artifact_path("plan"), "plan", "Article plan")
    add_source_refs(manifest, notes)
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = (
        f"Write the complete piece — every section of the plan, in order, "
        f"as one coherent document.\n\n"
        f"{manifest.render()}\n"
        f"Use list_research to browse all research findings, then "
        f"read_finding for details.\n\n"
    )
    task += author_context_block(notes)
    if format_guidance:
        task += f"{format_guidance}\n\n"
    task += (
        f"Write the draft to: {output_path}\n\n"
        f"Build it incrementally: Write the file with the opening, then "
        f"append each subsequent section with Edit. If you have questions "
        f"for the author, call note_for_author."
    )

    await query(
        task,
        model=stage_model("write"),
        system_prompt=SINGLE_WRITER_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        prefix="[write] ",
        trace_logger=trace_logger,
        heartbeat=heartbeat,
        cost_accumulator=cost_accumulator,
    )

    if not output_path.exists():
        raise PipelineError("Single writer produced no output (draft not written)")
    return output_path.read_text(encoding="utf-8")


async def merge_sections(
    section_draft_paths: dict[str, Path],
    *,
    notes: PipelineNotes,
    output_path: Path,
    voice_file_paths: list[str] | None = None,
    feedback_path: Path | None = None,
    target_format: str = "auto",
    author_notes: list[AuthorNote] | None = None,
    glossary_servers: dict[str, McpServerEntry] | None = None,
    glossary_tool_names: list[str] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    heartbeat: HeartbeatCallback | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> MergedDraft:
    """Stage 4: Assemble parallel sections into one draft via a voice-safe pass.

    Enforces the shared glossary, stitches seams, and cuts redundant openings —
    no structural rewrite, no voice smoothing. There is no separate planning
    phase: the glossary already carries the cross-section term decisions.
    """
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("merge", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
    merge_servers = {
        **note_servers,
        **(glossary_servers or {}),
        **(source_servers or {}),
        **(compute_servers or {}),
    }
    merge_tools = (
        note_tool_names
        + (glossary_tool_names or [])
        + (source_tool_names_list or [])
        + (compute_tool_names or [])
    )

    plan_path = notes.artifact_path("plan")
    ordered = list(section_draft_paths.items())
    drafts_list = "\n".join(
        f"{i + 1}. {title}: {path}" for i, (title, path) in enumerate(ordered)
    )

    manifest = ContentManifest()
    for title, path in ordered:
        manifest.add(
            path, "draft", f"Section: {title}", instruction="read each individually"
        )
    add_source_refs(manifest, notes)
    manifest.add(plan_path, "plan", "Article plan")
    add_voice_refs(manifest, voice_file_paths or [])
    if feedback_path:
        manifest.add(feedback_path, "feedback", "Author feedback")

    format_guidance = get_format_guidance(target_format)
    task = (
        f"Assemble these independently-written sections into one continuous "
        f"piece, in this order:\n{drafts_list}\n\n"
        f"{manifest.render()}\n\n"
    )
    task += author_context_block(notes)
    if format_guidance:
        task += f"{format_guidance}\n\n"
    task += (
        f"Call lookup_terms to read the shared glossary, then read each "
        f"section draft in order. Assemble them into one document: enforce the "
        f"glossary's canonical terms, stitch the seams between sections, and "
        f"cut only redundant re-openings. Do not rewrite, reorganize, or "
        f"smooth the author's voice.\n\n"
        f"Write the assembled piece to: {output_path}\n\n"
    )
    task += academic_assembly_block(target_format, output_path)

    await query(
        task,
        model=stage_model("merge"),
        system_prompt=MERGE_PROMPT,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=merge_servers,
        allowed_tools=merge_tools,
        trace_logger=trace_logger,
        prefix="[merge] ",
        heartbeat=heartbeat,
        cost_accumulator=cost_accumulator,
    )

    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
    else:
        raise PipelineError("Merge stage produced no output")

    return MergedDraft(content=content, changes_made=[])


def tab_id_for(tabs: StringMap, name: str) -> str:
    """The tab recorded under `name`, or empty where the doc has no such tab.

    The tab maps are keyed by titles the plan and the author invent, so a miss
    is ordinary rather than exceptional and every caller branches on the empty
    string it gets back.
    """
    return tabs[name] if name in tabs else ""


def load_review(review_path: Path) -> ReviewOutput:
    """What a reviewer recorded, or nothing where the stage wrote no file.

    Every reviewer persists through a `ReviewCollector`, so the artifact is a
    `ReviewOutput` whichever reviewer produced it — a reviewer that recorded
    no findings and one that never ran read the same here.
    """
    if not review_path.exists():
        return ReviewOutput(findings=[])
    return ReviewOutput.model_validate_json(review_path.read_text(encoding="utf-8"))


async def review_narrative(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for narrative coherence."""
    plan_path = notes.artifact_path("plan")
    review_path = notes.artifacts_dir / "review_narrative.json"
    collector = ReviewCollector(review_path, "narrative")
    outputs = build_output_server("output", make_review_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("review:narrative", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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

    voice_manifest = ContentManifest()
    add_voice_refs(voice_manifest, voice_file_paths or [])
    voice_block = f"{voice_manifest.render()}\n\n" if voice_file_paths else ""
    task = (
        f"Review the article draft for narrative coherence.\n\n"
        f"Draft: {draft_path}\n"
        f"Plan: {plan_path}\n\n"
        f"{voice_block}"
        f"{render_source_lines(notes)}"
        f"{directions_block(notes)}"
        f"Read both files. When you recommend a structural change, it must "
        f"not flatten the author's voice — read the voice files first, and "
        f"don't trade the author's self-implication, hedges, or asides for "
        f"conventional momentum. Call record_finding for each issue."
    )
    await query(
        task,
        model=stage_model("review"),
        system_prompt=NARRATIVE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:narrative] ",
        cost_accumulator=cost_accumulator,
    )
    return load_review(review_path)


async def review_facts(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    servers: dict[str, McpServerEntry] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for factual accuracy (needs research tools to verify)."""
    if servers is None:
        servers = build_research_servers()

    review_path = notes.artifacts_dir / "review_factcheck.json"
    collector = ReviewCollector(review_path, "factcheck")
    outputs = build_output_server("output", make_review_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("review:facts", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
        f"Draft: {draft_path}\n"
        f"Plan (includes the deliverable contract): {notes.artifact_path('plan')}\n\n"
        f"{render_source_lines(notes)}"
        f"{author_context_block(notes)}"
        f"Read the draft. Use list_research to see all research findings, "
        f"then read_finding for details on specific ones. Cross-reference "
        f"the draft against research findings, then verify remaining claims "
        f"with your research tools. Call record_finding for each issue."
    )
    await query(
        task,
        model=stage_model("review"),
        system_prompt=FACT_CHECKER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:facts] ",
        cost_accumulator=cost_accumulator,
    )
    return load_review(review_path)


async def review_style(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Review for writing quality and voice consistency."""
    review_path = notes.artifacts_dir / "review_style.json"
    collector = ReviewCollector(review_path, "style")
    outputs = build_output_server("output", make_review_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("review:style", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
        f"{author_context_block(notes)}"
        f"Read each voice and style reference file, then read the draft. "
        f"Call record_finding for each issue."
    )
    await query(
        task,
        model=stage_model("review"),
        system_prompt=STYLE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:style] ",
        cost_accumulator=cost_accumulator,
    )
    return load_review(review_path)


async def review_source_fidelity(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Verify the draft against the registered source documents."""
    review_path = notes.artifacts_dir / "review_source_fidelity.json"
    collector = ReviewCollector(review_path, "source_fidelity")
    outputs = build_output_server("output", make_review_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("review:source", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
        f"Verify the draft against the source documents.\n\n"
        f"Draft: {draft_path}\n"
        f"Plan: {notes.artifact_path('plan')}\n\n"
        f"{render_source_lines(notes)}"
        f"{author_context_block(notes)}"
        f"Check every source-derived definition, convention, named "
        f"structure, and argument outline against the document itself. "
        f"Call record_finding for each issue."
    )
    await query(
        task,
        model=stage_model("review"),
        system_prompt=SOURCE_FIDELITY_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:source] ",
        cost_accumulator=cost_accumulator,
    )
    return load_review(review_path)


async def review_coverage(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> ReviewOutput:
    """Flag the author's concrete specifics that the draft dropped or replaced."""
    review_path = notes.artifacts_dir / "review_coverage.json"
    collector = ReviewCollector(review_path, "coverage")
    outputs = build_output_server("output", make_review_output_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("review:coverage", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
    manifest.add(notes.artifact_path("plan"), "plan", "Article plan")
    add_source_refs(manifest, notes)
    add_voice_refs(manifest, voice_file_paths or [])

    task = (
        f"Check the draft for the author's concrete specifics that went "
        f"missing.\n\n{manifest.render()}\n\n"
        f"{author_context_block(notes)}"
        f"Read the plan (source_quotes, each section's quotes_to_include, and "
        f"the concrete nouns in its key_points) and the draft. Where a source "
        f"document is listed, scan it for named specifics too. Call "
        f"record_finding for each specific that was dropped, substituted with "
        f"a generic version, or hollowed out."
    )
    await query(
        task,
        model=stage_model("review"),
        system_prompt=COVERAGE_REVIEWER_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
        mcp_servers=all_servers,
        allowed_tools=all_tools,
        trace_logger=trace_logger,
        prefix="[review:coverage] ",
        cost_accumulator=cost_accumulator,
    )
    return load_review(review_path)


async def review_all(
    notes: PipelineNotes,
    draft_path: Path,
    *,
    servers: dict[str, McpServerEntry] | None = None,
    voice_file_paths: list[str] | None = None,
    author_notes: list[AuthorNote] | None = None,
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[ReviewFinding]:
    """Stage 5: Run the reviewers in parallel — narrative, fact-check, style,
    and coverage (the author's dropped specifics), plus source fidelity when
    source documents are registered."""
    narrative_task = review_narrative(
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

    coverage_task = review_coverage(
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

    review_tasks = [narrative_task, facts_task, style_task, coverage_task]
    if load_source_registry(registry_path_for(notes.artifacts_dir)):
        review_tasks.append(
            review_source_fidelity(
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
        )

    results = await asyncio.gather(*review_tasks, return_exceptions=True)

    def reported() -> Iterator[ReviewFinding]:
        """Findings from the reviewers that returned; a failure is logged, not raised,
        so one reviewer going down does not cost the others their findings."""
        for result in results:
            if isinstance(result, BaseException):
                logger.error("Reviewer failed: %s", result)
                continue
            yield from result.findings

    return list(reported())


class ConsolidatedFindings(BaseModel):
    """What survived the budget, and how much of it did not."""

    findings: list[ReviewFinding]
    dropped_suggestions: int


class StandbyWake(BaseModel):
    """Why standby woke: an author revision, a sync request, or both."""

    revision: str | None
    from_sync: bool


class StyleSamples(BaseModel):
    """The prose of the style references that could be read, and their labels."""

    prose: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


def consolidate_findings(
    findings: list[ReviewFinding],
    max_suggestions: int = 15,
) -> ConsolidatedFindings:
    """Deduplicate and budget review findings to prevent cumulative smoothing."""
    critical = [f for f in findings if f.severity == "critical"]
    praise = [f for f in findings if f.severity == "praise"]

    def deduplicated() -> Iterator[ReviewFinding]:
        """Suggestions, with later ones quoting the same passage folded into the
        first — two reviewers flagging one sentence is one thing to fix."""
        seen: dict[str, ReviewFinding] = {}  # lup: ignore[empty-collection] — a fold
        for f in findings:
            if f.severity in ("critical", "praise"):
                continue
            if f.text_excerpt:
                key = f.text_excerpt[:200]
                if key in seen:
                    first = seen[key]
                    first.suggestion = (
                        f"{first.suggestion}\n[Also from {f.reviewer}]: {f.suggestion}"
                    )
                    continue
                seen[key] = f
            yield f

    suggestions = list(deduplicated())

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
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> WritingOutput:
    """Stage 6: Incorporate all feedback and produce the final article."""
    note_collector = author_notes if author_notes is not None else []
    note_group = build_note_server("rewrite", note_collector)
    note_servers, note_tool_names = note_group.servers, note_group.tool_names
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
    resolutions_path = notes.artifacts_dir / "resolutions.md"
    if resolutions_path.exists():
        manifest.add(
            resolutions_path,
            "review",
            "Source resolutions",
            instruction=(
                "answers read from the author's brief and the source — apply "
                "as corrections; they outrank other inputs on correctness"
            ),
        )
    add_source_refs(manifest, notes)
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
    task += author_context_block(notes)
    task += (
        f"{manifest.render()}\n\n"
        f"Read the annotated draft — review findings are marked inline. "
        f"Passages marked [PRESERVE:] were praised by reviewers: protect "
        f"their quality while editing around them. "
        f"Apply all critical findings and worthwhile suggestions. "
        f"Use list_research + read_finding to verify corrections against research findings. "
        f"If a style rules file is available, read it and enforce every "
        f"hard editing rule with zero remaining violations.\n\n"
        f"Write the final piece to: {output_path}\n\n"
    )
    task += academic_assembly_block(target_format, output_path)

    collector = await query(
        task,
        model=stage_model("rewrite"),
        system_prompt=REWRITER_SYSTEM,
        tools=BUILTIN_WRITE_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
    summary = result_text(collector).strip()

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
    source_servers: dict[str, McpServerEntry] | None = None,
    source_tool_names_list: list[str] | None = None,
    compute_servers: dict[str, McpServerEntry] | None = None,
    compute_tool_names: list[str] | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> AssumptionsList:
    """Surface uncertainties and questions as GDoc comments."""
    plan_path = notes.artifact_path("plan")
    assumptions_path = notes.artifact_path("assumptions")
    collector = AssumptionsCollector(assumptions_path)
    outputs = build_output_server("output", make_assumptions_tools(collector))
    output_servers, output_tool_names = outputs.servers, outputs.tool_names

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
        model=stage_model("assumptions"),
        system_prompt=ASSUMPTIONS_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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

    if result.items:
        specs = [
            CommentSpec(
                content=(
                    f"{ASSUMPTION_TAG_PREFIX[item.tag]} {item.content}\n\n"
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
        model=stage_model("orchestrate"),
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=BUILTIN_READ_TOOLS,
        max_thinking_tokens=128_000 - 1,
        autonomy="unattended",
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
        stop_after: str | None = None,
    ) -> None:
        self.sources = sources
        self.refs = refs or []
        self.style_refs: list[str] = []
        self.context_refs: list[str] = list(self.refs)
        self.target_format = target_format
        self.existing_doc_id = existing_doc_id
        self.state = session_state or WritingSessionState()
        self.notes = notes
        self.trace_logger = trace_logger
        self.hooks = listener or PipelineListener()
        self.stop_after = validate_stop_after(stop_after)

        if cost_accumulator is None:
            cost_accumulator = CostAccumulator()
        self.cost_accumulator = cost_accumulator

        self.snapshot = PipelineSnapshot()
        self.pending_resume: StringMap = {}
        self.plan_breaking = asyncio.Event()
        self.restart_count = 0
        self.max_restarts = 2

        self.author_notes: list[AuthorNote] = []
        self.research_servers = build_research_servers()
        source_tools = build_source_server(
            lambda: registry_path_for(self.ensure_notes().artifacts_dir)
        )
        self.source_servers = source_tools.servers
        self.source_tool_names = source_tools.tool_names

        self.doc_id = ""
        self.doc_url = ""
        self.known_tabs: StringMap = {}
        self.tab_ids: StringMap = {}
        self.tabs: TabTracker | None = None

        self.overview_tab_id = ""
        self.draft_tab_id = ""
        self.final_tab_id = ""

        self.watcher: PollingWatcher | None = None
        self.source_watcher: PollingWatcher | None = None
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
        from lup.workspace.content_safety import configure

        configure(directory=self.notes.base_dir / "content")
        if self.state.agent_ids_path is None:
            self.state.attach_agent_ids_registry(self.notes.base_dir / "agent_ids.txt")
        return self.notes

    def source_mounts(self) -> dict[Path, str]:
        """Author source files, mounted read-only at their own host paths."""
        return {
            path: str(path)
            for src in self.sources
            if (path := Path(src).expanduser().resolve()).is_file()
        }

    def stage_docker_image(self) -> str:
        """Pick the sandbox image, building the academic image once if needed."""
        if self.effective_format == "academic" and not sandbox_image_available():
            logger.info("Academic format — building the sandbox image (one-time)")
            ensure_sandbox_image()
        if sandbox_image_available():
            return INKWELL_SANDBOX_IMAGE
        return Sandbox.DEFAULT_DOCKER_IMAGE

    def fallback_compute(self) -> ToolServers:
        """Compute server without code execution, for when Docker is absent."""
        from inkwell.agent.tools.citations import CITATION_TOOLS

        notes = self.ensure_notes()
        return build_output_server(
            "compute", [*make_query_tools(notes.artifacts_dir), *CITATION_TOOLS]
        )

    @asynccontextmanager
    async def stage_compute(self, label: str) -> AsyncIterator[StageCompute]:
        """Run one stage (or one parallel task) in its own fresh container.

        Inputs are mounted read-only at /notes, /shared carries scratch and
        cross-stage artifacts, and a per-stage output directory is mounted
        read-write at its identical host path — so the deliverable path is
        writable from both the host Write tool and execute_code, with no
        host/container translation. Yields the compute MCP server, its tool
        names, and the deliverable path; the container is stopped on exit.
        Falls back to a no-execution compute server when Docker is absent.
        """
        notes = self.ensure_notes()
        shared_dir = notes.artifacts_dir / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        out_dir = notes.work_dir / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / "output.md"
        sandbox = Sandbox(
            session_id=f"inkwell-{label}-{out_dir.name}",
            shared_dir=shared_dir,
            docker_image=self.stage_docker_image(),
            read_only_mounts={notes.base_dir: "/notes", **self.source_mounts()},
            rw_mounts={out_dir: str(out_dir)},
        )
        try:
            sandbox.start()
        except Exception:
            # Docker absent or refusing the container: the stage still runs,
            # with a compute server that cannot execute code.
            logger.exception("Sandbox unavailable for stage %s", label)
            fallback = self.fallback_compute()
            yield StageCompute(
                servers=fallback.servers,
                tool_names=fallback.tool_names,
                output_path=output_path,
                sandbox=None,
            )
            return
        if self.state is not None:
            self.state.shared_dir = shared_dir
        try:
            compute = build_compute_server(
                sandbox,
                notes.artifacts_dir,
                include_latex=self.effective_format == "academic",
            )
            yield StageCompute(
                servers=compute.servers,
                tool_names=compute.tool_names,
                output_path=output_path,
                sandbox=sandbox,
            )
        finally:
            sandbox.stop()

    async def finalize_on_interrupt(self) -> None:
        """Ship whatever the snapshot holds when an interrupt cuts the run short.

        Writes the content to a local file first — guaranteed even when the
        interrupt struck during the Google Doc write — then attempts the Final
        tab. A later resume re-runs format idempotently.
        """
        output = self.snapshot.output
        if output is None or not output.content:
            logger.warning(
                "Interrupt before any output was produced; nothing to finalize"
            )
            return
        notes = self.ensure_notes()
        local_path = notes.artifacts_dir / "final_on_interrupt.md"
        local_path.write_text(output.content, encoding="utf-8")
        logger.info("Interrupt: saved final content to %s", local_path)
        async with gdoc_nonfatal("write final tab on interrupt"):
            from inkwell.agent.tools.google_docs import write_with_continuation

            tab_ids = await write_with_continuation(
                self.doc_id, "Final", output.content, session_state=self.state
            )
            if tab_ids:
                self.final_tab_id = tab_ids[0]
                self.known_tabs["Final"] = tab_ids[0]

    def get_draft_path(self, label: str) -> Path:
        return self.ensure_notes().drafts_dir / f"{slugify(label)}.md"

    def rehydrate_draft_files(self) -> None:
        """Project the snapshot's draft content back onto disk for a resume.

        Section, merged, and final drafts are working files under ``drafts/``,
        not persisted artifacts: a resumed process starts without them. Stages
        downstream of ``write`` read these drafts as files, so a resume that
        skipped their producing stage would hand the reader a path to nothing.
        Restoring them from the snapshot keeps disk and snapshot in step no
        matter which stage the resume picks up from.
        """

        def restore(label: str, content: str) -> None:
            path = self.get_draft_path(label)
            if content and not path.exists():
                path.write_text(content, encoding="utf-8")

        for title, draft in self.snapshot.section_drafts.items():
            if not draft.content.startswith("[Section failed"):
                restore(title, draft.content)
        if self.snapshot.merged is not None:
            restore("merged", self.snapshot.merged.content)
        if self.snapshot.output is not None:
            restore("final", self.snapshot.output.content)

    def rehydrate_artifacts(self) -> None:
        """Project the snapshot's typed artifacts back onto disk for a restart.

        A restart rewinds to a checkpoint that predates the redone stage, but
        the on-disk artifacts still hold whatever a later stage last wrote (e.g.
        refine overwrites plan.json). Stages read their upstream inputs as
        files, so the artifacts are reset to the rewound snapshot's state —
        otherwise a redone stage would read newer state the rewind discarded.
        """
        notes = self.ensure_notes()
        if self.snapshot.plan is not None:
            notes.save_artifact("plan", self.snapshot.plan)
        if self.snapshot.research is not None:
            notes.save_artifact("research", self.snapshot.research)

    def rehydrate_sections(self) -> None:
        """Rebuild the section→tab map and section roster when resuming.

        create_section_tabs runs inside the plan and refine stages; a resume
        that picks up at or after write skips them, so self.tab_ids and the
        section roster would start empty — leaving section writers with no tab
        to sync into and the progress badge stuck at zero. setup_doc has
        already listed the doc's tabs into known_tabs, so the map is rebuilt
        from the snapshot's plan, with each section's drafted status read back
        from the persisted drafts.
        """
        plan = self.snapshot.plan
        if plan is None:
            return
        for i, section in enumerate(plan.sections):
            tab_name = truncate_tab_title(f"§{i + 1} {section.title}")
            tid = tab_id_for(self.known_tabs, tab_name)
            if tid:
                self.tab_ids[section.title] = tid
            drafted = self.section_already_drafted(section.title)
            self.state.add_section(
                section.title, tid, status="drafted" if drafted else "planned"
            )

    def write_stage_produced_nothing(self) -> bool:
        """True when the write stage recorded only failure placeholders.

        A transient writer failure (a usage or rate limit) records a
        ``[Section failed: ...]`` placeholder for every section yet still
        advances the snapshot to ``write``. Merge would then receive no
        usable sections. Detecting this lets the writer gate halt, and a
        resume rewind to re-run ``write`` once the limit clears.
        """
        drafts = self.snapshot.section_drafts
        return bool(drafts) and all(
            draft.content.startswith("[Section failed") for draft in drafts.values()
        )

    def section_already_drafted(self, title: str) -> bool:
        """True when a section already has a usable draft in the snapshot.

        Section writers persist each finished draft as they go, so a resumed
        write stage can skip the sections that completed before the
        interruption instead of paying to redraft them. A
        ``[Section failed: ...]`` placeholder does not count — those retry.
        """
        if title not in self.snapshot.section_drafts:
            return False
        draft = self.snapshot.section_drafts[title]
        return not draft.content.startswith("[Section failed")

    async def save_snapshot(self) -> None:
        self.snapshot.doc_id = self.state.doc_id
        self.snapshot.doc_url = self.state.doc_url
        self.snapshot.source_doc_id = self.state.source_doc_id
        self.snapshot.seen_comment_ids = list(self.state.seen_comments.handled)
        self.snapshot.agent_comment_ids = list(self.state.agent_comments.handled)
        self.snapshot.seen_source_comment_ids = list(
            self.state.seen_source_comments.handled
        )
        self.snapshot.pending_questions = list(self.state.pending_questions)
        self.snapshot.cost_state = self.cost_accumulator

        notes = self.ensure_notes()
        data = self.snapshot.model_dump_json()
        base = notes.base_dir
        base.mkdir(parents=True, exist_ok=True)
        (base / "snapshot.json").write_text(data, encoding="utf-8")
        (base / f"snapshot_{self.snapshot.stage}.json").write_text(
            data, encoding="utf-8"
        )

    async def announce_stage(self, stage: str, description: str) -> None:
        """Notify the listener and write a stage boundary into the trace.

        Trace files interleave every agent's blocks; without explicit
        markers, stage extents must be reverse-engineered from content.
        """
        if self.trace_logger is not None:
            self.trace_logger.log_text(description, heading=f"🚩 Stage: {stage}")
        await self.hooks.on_stage(stage, description)

    def install_block_callback(self) -> None:
        """Set the contextvar so all query() calls forward blocks to the listener."""

        async def forward_block(block: AnyTurnBlock, prefix: str) -> None:
            info = extract_block_info(block.telemetry_block)
            await self.hooks.on_block(info.label, info.content, prefix)

        active_block_callback.set(forward_block)

    def session_policy_for_run(self) -> SessionPolicy:
        """Route nested agents through resumable, persisted SDK sessions.

        Each call's session id is captured under its stage label and, on a
        resumed run, handed back so the agent continues its own conversation
        instead of restarting from a blank context. The cwd is pinned to the
        project root so the transcript namespace matches across resume.
        """
        return SessionPolicy(
            cwd=str(find_project_root()),
            resume_for=self.resume_session_lookup,
            capture=self.record_session,
        )

    def resume_session_lookup(self, label: str) -> str | None:
        """Hand back a stored session id once, to continue an interrupted agent.

        Popped so a within-process re-run (a restart or replan) starts fresh
        rather than extending the pre-interruption conversation.
        """
        return self.pending_resume.pop(label, None)

    async def record_session(self, label: str, session_id: str) -> None:
        """Persist a resumable agent's SDK session id the moment it is known."""
        if not is_resumable_label(label):
            return
        recorded = self.snapshot.session_ids
        if label in recorded and recorded[label] == session_id:
            return
        self.snapshot.session_ids[label] = session_id
        await self.save_snapshot()

    def prepare_resumable_sessions(self, snapshot: PipelineSnapshot) -> StringMap:
        """Session ids to resume, mirroring transcripts across a profile switch.

        Same profile: transcripts already sit under the active config dir.
        Different profile (e.g. resuming on another account after the first ran
        out of credit): the recorded transcripts are copied from the original
        profile's config dir into the current one, so the new login continues
        each conversation rather than restarting it.
        """
        if not snapshot.session_ids:
            return {}
        current = current_settings()
        if current.profile == snapshot.profile:
            return dict(snapshot.session_ids)
        placed = relocate_transcripts(
            load_settings(snapshot.profile).claude_config_dir,
            current.claude_config_dir,
            snapshot.session_ids,
        )
        if placed:
            logger.info(
                "Relocated %d session transcript(s) from profile %r for resume",
                len(placed),
                snapshot.profile,
            )
        return placed

    async def run_stage(self, stage_name: str) -> None:
        """Run one backbone stage and its standard boundary work.

        Dispatches to ``stage_<name>``, runs the restart check for the stages
        that can ingest plan-breaking feedback, polls for any requested sync,
        then honors a configured stop point. Centralizing the boundary keeps
        the fresh run and the resume loop in lockstep and gives the stop point
        one unmissable place to fire.
        """
        method = getattr(self, f"stage_{stage_name}")
        await method()
        if stage_name in ("research", "write", "review"):
            await self.check_and_maybe_restart()
        await self.sync_if_requested()
        self.maybe_stop(stage_name)

    def maybe_stop(self, stage: str) -> None:
        """Raise PipelineStopRequested if this stage is the configured stop point.

        Called only at a stage boundary, after the stage has finished and saved
        its snapshot, so the pause leaves a clean resume checkpoint.
        """
        if self.stop_after is not None and stage == self.stop_after:
            raise PipelineStopRequested(stage)

    async def handle_pause(self, stage: str, message: str = "") -> WritingOutput:
        """Finalize a clean pause at a configured stop point.

        The stage's snapshot is already saved, so a resume continues from the
        next stage. Refreshes the Overview tab, tells the listener so the
        environment can surface the pause and how to resume, and returns a
        marker output (``paused_after`` set, no finished content) so the caller
        reports a pause rather than a completion. ``message`` overrides the
        default summary when the halt has a specific reason (e.g. a missing
        source), so the author sees why rather than a generic stage name.
        """
        logger.info(
            "Paused after stage %r — %s", stage, message or "stop point reached"
        )
        await self.update_overview(paused=True)
        await self.hooks.on_pause(stage, self.doc_url)
        plan = self.snapshot.plan
        return WritingOutput(
            title=plan.title if plan else self.state.title,
            google_doc_id=self.doc_id,
            google_doc_url=self.doc_url,
            paused_after=stage,
            summary=message
            or (
                f"Paused after the {stage} stage. Review the Google Doc and "
                "comment, then resume to continue."
            ),
        )

    async def run(self) -> WritingOutput:
        """Execute the full pipeline with restart and stop-point support."""
        self.install_block_callback()
        self.snapshot.profile = current_settings().profile
        configure_session_state(self.state)
        await self.setup_doc()
        policy_token = session_policy.set(self.session_policy_for_run())

        try:
            for stage_name in ("extract", "voice", "plan"):
                await self.run_stage(stage_name)
            await self.run_stage("research")
            for stage_name in ("assumptions", "refine"):
                await self.run_stage(stage_name)

            await self.start_watcher()

            try:
                for stage_name in (
                    "write",
                    "merge",
                    "review",
                    "resolve",
                    "rewrite",
                    "format",
                ):
                    await self.run_stage(stage_name)

                output = self.snapshot.output
                if output is None:
                    raise PipelineError("Pipeline completed without producing output")

                await self.update_overview()
                await self.hooks.on_complete(output)

                await self.standby_loop()
            finally:
                await self.stop_watcher()
        except PipelineStopRequested as stop:
            return await self.handle_pause(stop.stage, stop.message)
        finally:
            session_policy.reset(policy_token)

        output = self.snapshot.output
        if output is None:
            raise PipelineError("Pipeline completed without producing output")
        return output

    async def run_from(
        self, snapshot: PipelineSnapshot, *, restart: bool = False
    ) -> WritingOutput:
        """Resume pipeline execution from a saved snapshot.

        With ``restart``, the snapshot is a checkpoint that predates the stage
        being redone: no agent resumes a prior conversation (``pending_resume``
        stays empty) and the on-disk artifacts are reset to the snapshot, so the
        redone stages regenerate from clean state instead of continuing one.
        """
        self.install_block_callback()
        self.snapshot = snapshot
        configure_session_state(self.state)

        if snapshot.doc_id:
            self.existing_doc_id = snapshot.doc_id
        elif snapshot.output and snapshot.output.google_doc_id:
            self.existing_doc_id = snapshot.output.google_doc_id

        self.state.seen_comments = CommentLedger(handled=snapshot.seen_comment_ids)
        self.state.agent_comments.mark_all(snapshot.agent_comment_ids)
        self.ensure_notes()
        self.rehydrate_draft_files()
        self.state.seen_source_comments = CommentLedger(
            handled=snapshot.seen_source_comment_ids
        )
        self.state.source_doc_id = snapshot.source_doc_id
        self.state.pending_questions = list(snapshot.pending_questions)
        if snapshot.cost_state is not None:
            self.cost_accumulator.merge(snapshot.cost_state)
        if restart:
            self.rehydrate_artifacts()
            self.pending_resume.clear()
        else:
            self.pending_resume = self.prepare_resumable_sessions(snapshot)

        await self.setup_doc()
        self.rehydrate_sections()
        policy_token = session_policy.set(self.session_policy_for_run())
        current_stage: str | None = None

        try:
            stages = ["preprocess", *DISPLAY_STAGES]
            last_idx = stages.index(snapshot.stage) if snapshot.stage in stages else -1
            remaining = [s for s in stages[last_idx + 1 :] if s != "preprocess"]
            if "write" not in remaining and self.write_stage_produced_nothing():
                remaining = stages[stages.index("write") :]

            if snapshot.plan:
                await self.start_watcher()

            try:
                for stage_name in remaining:
                    current_stage = stage_name
                    await self.run_stage(stage_name)

                output = self.snapshot.output
                if output is None:
                    raise PipelineError(
                        "Pipeline ran every stage but produced no final output"
                    )

                await self.update_overview()
                await self.hooks.on_complete(output)
                await self.standby_loop()
            except PipelineStopRequested as stop:
                return await self.handle_pause(stop.stage, stop.message)
            except (
                Exception
            ) as exc:  # claude: ignore — SDK raises generic Exception for exit codes
                if not is_interrupt(exc):
                    raise
                logger.info(
                    "Pipeline interrupted during stage %r — finalizing",
                    current_stage,
                )
                await self.finalize_on_interrupt()
            finally:
                await self.stop_watcher()
        finally:
            session_policy.reset(policy_token)

        output = self.snapshot.output
        if output is not None:
            return output
        raise PipelineInterrupted(current_stage)

    async def start_watcher(self) -> None:
        if self.notes is None:
            return
        self.watcher = create_comment_watcher(
            session_state=self.state,
            notes=self.notes,
            plan_breaking_signal=self.plan_breaking,
        )
        await self.watcher.start()
        logger.info("Comment watcher started")

        if self.state.source_doc_id and not self.source_is_extracted_gdoc:
            self.source_watcher = create_source_watcher(
                session_state=self.state,
                notes=self.notes,
                plan_breaking_signal=self.plan_breaking,
            )
            await self.source_watcher.start()
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
            created = await do_create_doc(
                f"Inkwell — {', '.join(s[:30] for s in self.sources)[:60]}",
                share_with=current_settings().author_email,
                session_state=self.state,
            )
            self.doc_id, self.doc_url = created.doc_id, created.url
            await self.hooks.on_progress(f"Google Doc: {self.doc_url}")

        for tab_name in ("Overview", "Source", "Voice", "Plan", "Research"):
            if tab_name not in self.known_tabs:
                tid = await do_create_tab(self.doc_id, tab_name)
                self.known_tabs[tab_name] = tid

        self.overview_tab_id = self.known_tabs["Overview"]
        if "Draft" in self.known_tabs:
            self.draft_tab_id = self.known_tabs["Draft"]
        if "Final" in self.known_tabs:
            self.final_tab_id = self.known_tabs["Final"]
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
            model=stage_model("classify"),
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
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

        reply_text = ACKNOWLEDGE_TEMPLATES[classified.impact]
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
            model=stage_model("classify"),
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
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
            model=stage_model("classify"),
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
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
            "Pre-existing comments from the source document. They come from "
            "the document's author and from reviewers, who often disagree. A "
            "reviewer's comment is input, not a settled decision: when a "
            "thread has a reply, the reply is the later word and resolves it "
            "— follow the reply over the comment it answers. Never cut or "
            "rewrite something a reviewer flags if the author defends keeping "
            "it in a reply. Weigh the author's own stated positions above any "
            "single reviewer's suggestion.\n"
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

    async def warn_missing_source(self, note: str) -> None:
        """Halt when the instructions reference source material that never
        arrived, rather than silently writing from the directions alone.

        The author pasted directions that lean on a document, file, or link, but
        no such source reached the pipeline (a dropped upload, an unsent paste).
        Surface it loudly and pause so they can re-attach and start again.
        """
        detail = f" ({note})" if note else ""
        await self.hooks.on_progress(
            "⚠ Your instructions reference source material that didn't arrive"
            f"{detail}. Only your directions came through — no document, file, "
            "or link. Pausing instead of writing from the instructions alone; "
            "re-create the session with the source attached, then start again."
        )
        raise PipelineStopRequested(
            "extract",
            "Paused: the instructions reference source material that wasn't "
            "provided. Re-create the session with the document attached.",
        )

    async def extract_style_samples(self) -> StyleSamples:
        """Extract the prose of runtime style references for voice analysis."""
        if not self.style_refs:
            return StyleSamples()
        results = await asyncio.gather(
            *(
                extract_single_source(url, self.existing_doc_id)
                for url in self.style_refs
            ),
            return_exceptions=True,
        )
        extracted = [
            (url, sample)
            for url, sample in zip(self.style_refs, results)
            if sample and not isinstance(sample, BaseException)
        ]
        return StyleSamples(
            prose=[sample for _url, sample in extracted],
            labels=[url for url, _sample in extracted],
        )

    async def stage_extract(self) -> None:
        await self.announce_stage("extract", "Extracting source material")
        await self.update_overview(active_stage="extract")

        notes = self.ensure_notes()
        source_dir = notes.artifacts_dir / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        output_path = source_dir / "conversation.md"

        original_inputs = list(self.sources) + list(self.refs)

        self_doc_sources = [
            s
            for s in self.sources
            if self.existing_doc_id
            and is_gdoc_url(s)
            and parse_gdoc_id(s) == self.existing_doc_id
        ]
        agent_inputs = [s for s in self.sources if s not in self_doc_sources]

        manifest = await run_extraction_agent(
            agent_inputs,
            source_dir,
            source_servers=self.source_servers,
            source_tool_names=self.source_tool_names,
            trace_logger=self.trace_logger,
            cost_accumulator=self.cost_accumulator,
        )
        notes.save_artifact("extract", manifest)

        await self.hooks.on_progress(
            "Inputs received: " + summarize_inputs(original_inputs)
        )
        if manifest.references_absent_source and not manifest.has_concrete_source:
            await self.warn_missing_source(manifest.absent_source_note)

        self.snapshot.raw_sources = list(self.sources)
        self.snapshot.author_instructions = manifest.instructions
        self.snapshot.author_deliverables = manifest.deliverables
        self.style_refs = [
            e.raw_input for e in manifest.sources if e.role == "style_reference"
        ]
        routed = [e.raw_input for e in manifest.sources if e.role == "source"]
        self.sources = (routed or agent_inputs) + self_doc_sources
        context = [e.raw_input for e in manifest.sources if e.role == "context"]
        self.context_refs = list(dict.fromkeys(self.context_refs + context))

        source_doc_id = ""
        for src in self.sources:
            if not source_doc_id and is_gdoc_url(src):
                source_doc_id = parse_gdoc_id(src)
                self.state.source_doc_id = source_doc_id
                self.source_is_extracted_gdoc = True

        extracted_parts, unrecovered = assemble_sources(manifest)
        covered = {e.raw_input for e in manifest.sources}
        unrecovered += [value for value in original_inputs if value not in covered]
        unrecovered = list(dict.fromkeys(unrecovered))
        source_set = {src for src in self.sources}
        if unrecovered:
            await self.hooks.on_progress(
                f"Deterministically extracting {len(unrecovered)} source(s) "
                f"the agent left unrecovered"
            )
            results = await asyncio.gather(
                *(
                    extract_single_source(url, self.existing_doc_id)
                    for url in unrecovered
                ),
                return_exceptions=True,
            )
            for url, result in zip(unrecovered, results):
                if isinstance(result, BaseException):
                    logger.warning("Extraction failed for %s: %s", url, result)
                elif result.strip():
                    role = "Source" if url in source_set else "Reference"
                    extracted_parts.append(f"--- {role}: {url} ---\n\n{result}")

        conversation = "\n\n".join(extracted_parts)
        output_path.write_text(conversation, encoding="utf-8")

        style = await self.extract_style_samples()
        self.snapshot.style_ref_samples = style.prose
        self.snapshot.runtime_style_refs = style.labels

        all_source_paths = [str(p) for p in sorted(source_dir.glob("*")) if p.is_file()]

        registry_candidates = [
            s for s in self.sources if Path(s).expanduser().is_file()
        ]
        if conversation.strip():
            registry_candidates.append(str(output_path))
        registered = await build_source_registry_async(
            registry_candidates, notes.artifacts_dir
        )
        if registered:
            await self.hooks.on_progress(
                "Source registry: "
                + ", ".join(
                    f"{d.label} ({d.page_count}p)" if d.page_count else d.label
                    for d in registered
                )
            )

        self.snapshot.conversation = conversation
        self.snapshot.source_file_paths = all_source_paths

        markers = extract_author_markers(conversation)
        if markers:
            marker_block = "\n".join(f"- {m}" for m in markers)
            self.snapshot.author_instructions = (
                f"{self.snapshot.author_instructions}\n\n"
                f"Unresolved markers the author left in the source — resolve "
                f"each (fill the number, run the estimate, rewrite the passage) "
                f"or surface it as a question, never silently drop it:\n"
                f"{marker_block}"
            ).strip()
            for m in markers:
                self.state.add_question(f"Unresolved source marker: {m}")
            await self.hooks.on_progress(
                f"Surfaced {len(markers)} unresolved author marker(s) from the source"
            )

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
        await self.announce_stage("voice", "Analyzing author's writing voice")
        await self.update_overview(active_stage="voice")
        corpus = await load_style_corpus() + [
            StyleSample(label=label, text=text, source_type="descriptive")
            for text, label in zip(
                self.snapshot.style_ref_samples, self.snapshot.runtime_style_refs
            )
        ]
        notes = self.ensure_notes()

        fingerprint = await compute_voice_fingerprint(
            self.snapshot.conversation,
            [sample.text for sample in corpus],
            self.target_format,
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

        explicit_prescriptive = [entry for entry in corpus if entry.prescriptive]
        analyzable = [entry for entry in corpus if not entry.prescriptive]

        async with self.stage_compute("voice") as sc:
            all_servers = {**self.research_servers, **sc.servers}
            all_tool_names = research_tool_names() + sc.tool_names
            analyses = await analyze_voice_individually(
                self.snapshot.conversation,
                analyzable,
                target_format=self.target_format,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
                mcp_servers=all_servers,
                mcp_tool_names=all_tool_names,
            )
        if not analyses and not explicit_prescriptive:
            raise PipelineError("Voice analysis produced no output")

        voice_file_paths: list[
            str
        ] = []  # lup: ignore[empty-collection] — a 3-loop fold
        voice_tab_parts: list[str] = []  # lup: ignore[empty-collection] — a 3-loop fold
        n_voice = 0
        n_auto_prescriptive = 0
        presc_idx = 0

        source_by_label: StringMap = {entry.label: entry.text for entry in analyzable}

        for one in analyses:
            label, text = one.label, one.analysis
            slug = slugify(label)
            if one.prescriptive and label in source_by_label:
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

        for entry in explicit_prescriptive:
            slug = slugify(entry.label)
            envelope = await notes.save_content(
                f"prescriptive_{presc_idx}_{slug}",
                entry.text,
                stage="voice",
                content_type="prescriptive",
                label=entry.label,
            )
            voice_file_paths.append(envelope.path)
            voice_tab_parts.append(f"## Prescriptive: {entry.label}\n\n{entry.text}")
            presc_idx += 1

        saved_verbatim = {one.label for one in analyses if one.prescriptive}
        for i, entry in enumerate(analyzable):
            if entry.label in saved_verbatim:
                continue
            slug = slugify(entry.label)
            envelope = await notes.save_content(
                f"corpus_{i}_{slug}",
                entry.text,
                stage="voice",
                content_type="source",
                label=entry.label,
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
        await self.announce_stage("plan", "Planning article structure")
        await self.update_overview(active_stage="plan")
        notes = self.ensure_notes()
        plan_path = notes.artifact_path("plan")

        for doc in load_source_registry(registry_path_for(notes.artifacts_dir)):
            if doc.kind != "pdf" or not doc.page_count:
                continue
            existing = list(
                reading_notes_dir(notes.artifacts_dir).glob(f"{doc.label}_p*.md")
            )
            if existing:
                continue
            await self.hooks.on_progress(
                f"Reading {doc.label} ({doc.page_count}p) — building reading notes"
            )
            built = await build_reading_notes(
                doc,
                notes.artifacts_dir,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
            await self.hooks.on_progress(
                f"Reading notes: {len(built)} window(s) for {doc.label}"
            )

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
        if self.snapshot.author_deliverables:
            items = "\n".join(f"- {d}" for d in self.snapshot.author_deliverables)
            directions = (
                f"{directions}\n\nDeliverables (the contract — copy into "
                f"set_plan_header verbatim):\n{items}"
            )

        brief = render_brief(
            self.snapshot.author_instructions, self.snapshot.author_deliverables
        )
        if brief:
            notes.save_brief(brief)

        async with self.stage_compute("plan") as sc:
            plan = await plan_article(
                notes,
                target_format=self.target_format,
                voice_file_paths=self.snapshot.voice_file_paths,
                source_file_paths=self.snapshot.source_file_paths,
                author_notes=self.author_notes,
                author_directions=directions,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
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
        await self.announce_stage("assumptions", "Surfacing questions for the author")
        await self.update_overview(active_stage="assumptions")
        notes = self.ensure_notes()

        async with self.stage_compute("assumptions") as sc:
            assumptions = await surface_assumptions(
                notes,
                self.doc_id,
                has_research=self.snapshot.research is not None,
                session_state=self.state,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
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

        await self.announce_stage(
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

        async with self.stage_compute("research") as sc:
            research = await research_plan(
                notes,
                feedback_path=feedback_path,
                servers=self.research_servers,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                author_notes=self.author_notes,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
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

        await self.announce_stage(
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

        async with self.stage_compute("refine") as sc:
            refined = await refine_plan(
                notes,
                voice_file_paths=self.snapshot.voice_file_paths,
                feedback_path=feedback_path,
                author_notes=self.author_notes,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
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

        if resolve_writer_mode(current_settings().writer_mode) == "single":
            await self.write_single_draft()
            return

        pending = [
            s for s in plan.sections if not self.section_already_drafted(s.title)
        ]
        already = len(plan.sections) - len(pending)
        detail = f"Writing {len(pending)} sections in parallel"
        if already:
            detail = f"Writing {len(pending)} remaining ({already} already drafted)"
        await self.announce_stage("write", detail)
        self.state.set_stage("writing")
        await self.update_overview(active_stage="write")

        feedback_path = await self.prepare_feedback("write")
        notes = self.ensure_notes()
        glossary_path = notes.artifacts_dir / "glossary.json"
        seed_glossary(glossary_path, plan.conventions)
        glossary = build_glossary_server(glossary_path)
        glossary_servers, glossary_tool_names = glossary.servers, glossary.tool_names

        completed = already

        async def write_and_publish(section: SectionPlan) -> SectionDraft:
            nonlocal completed
            tid = tab_id_for(self.tab_ids, section.title)
            draft_path = self.get_draft_path(section.title)
            async with self.stage_compute(f"write:{section.title}") as sc:
                syncer: DraftSyncer | None = None
                if tid:
                    syncer = DraftSyncer(
                        self.doc_id,
                        tid,
                        sc.output_path,
                        session_state=self.state,
                    )
                    await syncer.start()
                try:
                    draft = await write_section(
                        section.title,
                        notes=notes,
                        draft_path=sc.output_path,
                        voice_file_paths=self.snapshot.voice_file_paths,
                        feedback_path=feedback_path,
                        section_context=self.build_neighbor_context(
                            plan, section.title
                        ),
                        target_format=self.effective_format,
                        servers=self.research_servers,
                        source_servers=self.source_servers,
                        source_tool_names_list=self.source_tool_names,
                        author_notes=self.author_notes,
                        compute_servers=sc.servers,
                        compute_tool_names=sc.tool_names,
                        glossary_servers=glossary_servers,
                        glossary_tool_names=glossary_tool_names,
                        trace_logger=self.trace_logger,
                        cost_accumulator=self.cost_accumulator,
                    )
                finally:
                    if syncer is not None:
                        await syncer.stop()
            draft_path.write_text(draft.content, encoding="utf-8")
            self.snapshot.section_drafts[section.title] = draft
            await self.save_snapshot()
            completed += 1

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
                f"[{completed}/{len(plan.sections)}] "
                f"'{draft.title}' complete ({draft.word_count} words)",
            )
            await self.update_overview(active_stage="write")
            await self.hooks.on_state_change()
            return draft

        results = await asyncio.gather(
            *(write_and_publish(s) for s in pending),
            return_exceptions=True,
        )

        for section, result in zip(pending, results):
            if isinstance(result, BaseException):
                logger.error("Section '%s' failed: %s", section.title, result)
                self.snapshot.section_drafts[section.title] = SectionDraft(
                    title=section.title,
                    content=f"[Section failed: {result}]",
                    word_count=0,
                )

        await self.post_author_notes()
        if self.write_stage_produced_nothing():
            sample = next(iter(self.snapshot.section_drafts.values()), None)
            raise PipelineError(
                "Write stage produced no usable sections: "
                + (sample.content if sample else "no sections")
            )
        self.snapshot.stage = "write"
        await self.save_snapshot()
        await self.update_overview()

    async def write_single_draft(self) -> None:
        """writer_mode='single': one writer drafts the whole piece, no merge."""
        await self.announce_stage("write", "Writing the full draft (single writer)")
        self.state.set_stage("writing")
        await self.update_overview(active_stage="write")
        feedback_path = await self.prepare_feedback("write")
        notes = self.ensure_notes()
        output_path = self.get_draft_path("merged")

        if "Draft" not in self.known_tabs:
            self.known_tabs["Draft"] = await do_create_tab(self.doc_id, "Draft")
        self.draft_tab_id = self.known_tabs["Draft"]

        async def write_heartbeat(elapsed: float) -> None:
            mins, secs = divmod(int(elapsed), 60)
            await self.hooks.on_progress(f"Writing... ({mins}m{secs:02d}s elapsed)")

        async with self.stage_compute("write") as sc:
            syncer = DraftSyncer(
                self.doc_id,
                self.draft_tab_id,
                sc.output_path,
                tab_name="Draft",
                session_state=self.state,
                interval=8.0,
            )
            await syncer.start()
            try:
                content = await write_full_draft(
                    notes=notes,
                    output_path=sc.output_path,
                    voice_file_paths=self.snapshot.voice_file_paths,
                    feedback_path=feedback_path,
                    target_format=self.effective_format,
                    servers=self.research_servers,
                    author_notes=self.author_notes,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    compute_servers=sc.servers,
                    compute_tool_names=sc.tool_names,
                    trace_logger=self.trace_logger,
                    heartbeat=write_heartbeat,
                    cost_accumulator=self.cost_accumulator,
                )
            finally:
                await syncer.stop()
        output_path.write_text(content, encoding="utf-8")

        await self.post_author_notes()
        self.snapshot.merged = MergedDraft(
            content=content,
            changes_made=["written in one pass by a single writer"],
        )
        self.snapshot.stage = "write"
        await self.save_snapshot()
        assert self.tabs is not None
        await self.tabs.record_from_doc(self.draft_tab_id, "Draft")
        await self.hooks.on_message(
            "write", f"Full draft complete ({len(content.split())} words)"
        )
        await self.update_overview()

    async def stage_merge(self) -> None:
        plan = self.snapshot.plan
        if plan is None:
            raise PipelineError("Cannot merge without a plan")

        if (
            resolve_writer_mode(current_settings().writer_mode) == "single"
            and self.snapshot.merged
        ):
            self.snapshot.stage = "merge"
            await self.save_snapshot()
            return

        await self.announce_stage("merge", "Merging sections into one draft")
        self.state.set_stage("merging")
        await self.update_overview(active_stage="merge")
        feedback_path = await self.prepare_feedback("merge")
        notes = self.ensure_notes()
        glossary = build_glossary_server(notes.artifacts_dir / "glossary.json")
        glossary_servers, glossary_tool_names = glossary.servers, glossary.tool_names

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
            self.known_tabs["Draft"] = await do_create_tab(self.doc_id, "Draft")
        self.draft_tab_id = self.known_tabs["Draft"]

        async with self.stage_compute("merge") as sc:
            syncer = DraftSyncer(
                self.doc_id,
                self.draft_tab_id,
                sc.output_path,
                tab_name="Draft",
                session_state=self.state,
                interval=8.0,
            )
            await syncer.start()
            try:
                merged = await merge_sections(
                    section_paths,
                    notes=notes,
                    output_path=sc.output_path,
                    voice_file_paths=self.snapshot.voice_file_paths,
                    feedback_path=feedback_path,
                    target_format=self.effective_format,
                    author_notes=self.author_notes,
                    glossary_servers=glossary_servers,
                    glossary_tool_names=glossary_tool_names,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    compute_servers=sc.servers,
                    compute_tool_names=sc.tool_names,
                    trace_logger=self.trace_logger,
                    heartbeat=merge_heartbeat,
                    cost_accumulator=self.cost_accumulator,
                )
            finally:
                await syncer.stop()
        output_path.write_text(merged.content, encoding="utf-8")
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

        await self.announce_stage("review", "Reviewing draft (parallel reviewers)")
        self.state.set_stage("reviewing")
        await self.update_overview(active_stage="review")

        notes = self.ensure_notes()
        draft_path = self.get_draft_path("merged")

        async with self.stage_compute("review") as sc:
            findings = await review_all(
                notes,
                draft_path,
                servers=self.research_servers,
                voice_file_paths=self.snapshot.voice_file_paths,
                author_notes=self.author_notes,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
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

    async def stage_resolve(self) -> None:
        """Answer source-answerable open questions before the rewrite.

        Writers and reviewers leave questions; the ones a source document
        can answer get answered here by reading it, so only judgment calls
        reach the author and the rewrite corrects from evidence instead of
        guessing.
        """
        notes = self.ensure_notes()
        if not load_source_registry(registry_path_for(notes.artifacts_dir)):
            return
        questions = list(dict.fromkeys(self.state.pending_questions))
        if not questions:
            return

        await self.announce_stage(
            "resolve",
            f"Resolving {len(questions)} open questions against the source",
        )
        resolutions_path = notes.artifacts_dir / "resolutions.md"
        items = "\n".join(f"- {q}" for q in questions)
        task = (
            f"Open questions left by writers and reviewers:\n\n{items}\n\n"
            f"{render_source_lines(notes)}"
            f"{author_context_block(notes)}"
            f"Resolve each question from the author's brief first, then the "
            f"source; for a genuine judgment call, mark it for the author with "
            f"a conservative brief-honoring default.\n\n"
            f"Write the resolutions to: {resolutions_path}"
        )
        async with self.stage_compute("resolve") as sc:
            await query(
                task,
                model=stage_model("review"),
                system_prompt=RESOLVER_PROMPT,
                tools=BUILTIN_WRITE_TOOLS,
                max_thinking_tokens=128_000 - 1,
                autonomy="unattended",
                mcp_servers={**self.source_servers, **sc.servers},
                allowed_tools=self.source_tool_names + sc.tool_names,
                prefix="[resolve] ",
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
        if resolutions_path.exists():
            resolved = resolutions_path.read_text(encoding="utf-8").count("\n  A:")
            await self.hooks.on_progress(
                f"Resolved {resolved} question(s) from the source; "
                f"resolutions saved for the rewrite"
            )

    async def stage_rewrite(self) -> None:
        plan = self.snapshot.plan
        merged = self.snapshot.merged
        if plan is None or merged is None:
            raise PipelineError("Cannot rewrite without plan and merged draft")

        await self.announce_stage("rewrite", "Producing final version")
        self.state.set_stage("rewriting")
        await self.update_overview(active_stage="rewrite")
        feedback_path = await self.prepare_feedback("rewrite")
        notes = self.ensure_notes()

        draft_path = self.get_draft_path("merged")
        output_path = self.get_draft_path("final")

        if "Draft" not in self.known_tabs:
            self.known_tabs["Draft"] = await do_create_tab(self.doc_id, "Draft")
        self.draft_tab_id = self.known_tabs["Draft"]

        async with self.stage_compute("rewrite") as sc:
            syncer = DraftSyncer(
                self.doc_id,
                self.draft_tab_id,
                sc.output_path,
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
                    output_path=sc.output_path,
                    voice_file_paths=self.snapshot.voice_file_paths,
                    feedback_path=feedback_path,
                    target_format=self.effective_format,
                    dropped_suggestions=getattr(self, "dropped_suggestions", 0),
                    author_notes=self.author_notes,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    compute_servers=sc.servers,
                    compute_tool_names=sc.tool_names,
                    trace_logger=self.trace_logger,
                    cost_accumulator=self.cost_accumulator,
                )
            finally:
                await syncer.stop()
        output_path.write_text(output.content, encoding="utf-8")
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
        await self.announce_stage("format", f"Applying {chosen_format} formatting")
        await self.update_overview(active_stage="format")
        article_text = output.content or output.summary

        if chosen_format == "academic":
            final_content = await self.finalize_academic(article_text, plan)
        else:
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

    async def finalize_academic(self, body: str, plan: ArticlePlan) -> str:
        """Compile and publish the paper the document-owning stages produced.

        Merge and rewrite emit a complete, compiling LaTeX document; this
        compiles it once more for the deliverable and publishes: the Final tab
        carries the raw .tex, a Preview tab shows the rasterized PDF, and both
        .tex and .pdf upload to Drive with their links surfaced.
        ``assemble_latex_document`` wraps a body-only draft as a fallback when
        an upstream stage did not own the whole document.
        """
        from inkwell.agent.tools.google_docs import (
            do_upload_artifact,
            write_with_continuation,
        )
        from inkwell.agent.tools.latex import (
            assemble_latex_document,
            compile_tex,
            save_artifacts_to,
        )

        tex_source = assemble_latex_document(body, plan.title)
        async with gdoc_nonfatal("write final tab"):
            tab_ids = await write_with_continuation(
                self.doc_id, "Final", tex_source, session_state=self.state, plain=True
            )
            if tab_ids:
                self.final_tab_id = tab_ids[0]
                self.known_tabs["Final"] = tab_ids[0]

        async with self.stage_compute("academic") as sc:
            if sc.sandbox is None:
                await self.hooks.on_progress(
                    "No sandbox — shipping .tex without compile"
                )
                return tex_source

            await self.hooks.on_progress("Compiling paper.tex (tectonic)")
            artifacts = await compile_tex(tex_source, sc.sandbox)
            artifacts = save_artifacts_to(
                artifacts, self.ensure_notes().artifacts_dir / "latex"
            )
            if artifacts.tex_path:
                async with gdoc_nonfatal("upload paper.tex"):
                    tex = await do_upload_artifact(
                        artifacts.tex_path, "application/x-tex"
                    )
                    await self.hooks.on_progress(f"Uploaded paper.tex: {tex.url}")
            if not artifacts.pdf_path:
                logger.warning("LaTeX compile incomplete: %s", artifacts.log[-300:])
                await self.hooks.on_progress(
                    "LaTeX compile incomplete — shipped .tex only"
                )
                return tex_source
            async with gdoc_nonfatal("upload paper.pdf"):
                pdf = await do_upload_artifact(artifacts.pdf_path, "application/pdf")
                await self.hooks.on_progress(f"Uploaded paper.pdf: {pdf.url}")
            await self.write_preview_tab(sc.sandbox)
            return tex_source

    async def write_preview_tab(self, sandbox: Sandbox | None) -> None:
        """Render the compiled PDF to images and publish them in a Preview tab."""
        from inkwell.agent.tools.google_docs import (
            do_create_tab,
            do_insert_image,
            do_write_tab,
            find_tab_by_title,
        )
        from inkwell.agent.tools.latex import render_pdf_preview

        if sandbox is None:
            return
        pages = await render_pdf_preview(sandbox)
        if not pages:
            return
        async with gdoc_nonfatal("write preview tab"):
            tab_id = await find_tab_by_title(self.doc_id, "Preview")
            if tab_id is None:
                tab_id = await do_create_tab(self.doc_id, "Preview")
            self.known_tabs["Preview"] = tab_id
            await do_write_tab(
                self.doc_id,
                tab_id,
                "Rendered preview of the compiled paper.\n\n",
                session_state=self.state,
                plain=True,
            )
            for page in pages:
                await do_insert_image(self.doc_id, tab_id, str(page), width_pts=460)

    # -- Sync / restart logic ----------------------------------------------

    async def sync_if_requested(self) -> None:
        if not self.hooks.sync_requested.is_set():
            return
        self.hooks.sync_requested.clear()
        logger.info("Sync requested — gathering feedback")
        await self.announce_stage("sync", "Polling comments, edits, and terminal input")
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
            await notes.downgrade_plan_breaking()
            return

        logger.info("Plan-breaking feedback detected — running orchestrator")
        await self.announce_stage(
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
        await notes.clear_plan_breaking()

    async def execute_strategy(self, strategy: RestartStrategy) -> None:
        notes = self.ensure_notes()
        if strategy.new_plan:
            self.snapshot.plan = strategy.new_plan
            notes.save_artifact("plan", strategy.new_plan)
            await self.write_plan_tab(strategy.new_plan)
            await self.create_section_tabs(strategy.new_plan)

        queue = RestartQueue()
        for action in strategy.actions:
            await action.carry_out(self, queue)

        if queue.pending():
            await self.execute_rewrites(queue)

        # Only remerge once the linear merge stage has already run: a restart
        # before it (research/write checkpoints) is picked up by the merge stage
        # ahead, so remerging here would just duplicate it. When the draft was
        # already reviewed, the existing findings now point at superseded text —
        # re-review so the rewrite applies findings that match the new draft.
        if (
            strategy.needs_remerge
            and self.snapshot.merged is not None
            and self.snapshot.section_drafts
        ):
            stale_findings = bool(self.snapshot.findings)
            await self.stage_merge()
            if stale_findings:
                await self.stage_review()

    async def patch_section(
        self, section: str, target_text: str, instruction: str
    ) -> None:
        if section not in self.snapshot.section_drafts:
            logger.warning("Cannot patch missing section: %s", section)
            return
        draft = self.snapshot.section_drafts[section]

        patch_path = self.get_draft_path(section)
        patch_path.parent.mkdir(parents=True, exist_ok=True)

        async with self.stage_compute(f"patch:{section}") as sc:
            sc.output_path.write_text(draft.content, encoding="utf-8")
            task = (
                f"Apply this targeted edit to the section.\n\n"
                f"## Section: {section}\n\n"
                f"Draft file: {sc.output_path}\n\n"
                f'## Edit Target\n\n"{target_text}"\n\n'
                f"## Instruction\n\n{instruction}\n\n"
                f"Read the draft with the Read tool, then use Edit to apply "
                f"the change. Use Write if the change is too large for Edit."
            )
            all_servers = {
                **self.research_servers,
                **self.source_servers,
                **sc.servers,
            }
            all_tools = research_tool_names() + self.source_tool_names + sc.tool_names
            await query(
                task,
                model=stage_model("write"),
                system_prompt=SECTION_WRITER_PROMPT,
                tools=BUILTIN_WRITE_TOOLS,
                max_thinking_tokens=128_000 - 1,
                autonomy="unattended",
                mcp_servers=all_servers,
                allowed_tools=all_tools,
                prefix=f"[patch:{section}] ",
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
            content = sc.output_path.read_text(encoding="utf-8")

        patch_path.write_text(content, encoding="utf-8")
        self.snapshot.section_drafts[section] = SectionDraft(
            title=section,
            content=content,
            word_count=len(content.split()),
        )

    async def execute_rewrites(self, queue: RestartQueue) -> None:
        plan = self.snapshot.plan
        if plan is None:
            return

        notes = self.ensure_notes()

        new_questions = [q for task in queue.rewrites for q in task.research_questions]
        if new_questions:
            await self.hooks.on_progress(
                f"Researching {len(new_questions)} new question(s) before rewriting"
            )
            async with self.stage_compute("restart:research") as sc:
                self.snapshot.research = await research_questions(
                    notes,
                    new_questions,
                    servers=self.research_servers,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    compute_servers=sc.servers,
                    compute_tool_names=sc.tool_names,
                    trace_logger=self.trace_logger,
                    cost_accumulator=self.cost_accumulator,
                )

        feedback_path = await self.prepare_feedback("restart")
        glossary = build_glossary_server(notes.artifacts_dir / "glossary.json")
        glossary_servers, glossary_tool_names = glossary.servers, glossary.tool_names

        async def rewrite_coro(section_plan: SectionPlan) -> SectionDraft:
            draft_path = self.get_draft_path(section_plan.title)
            async with self.stage_compute(f"rewrite:{section_plan.title}") as sc:
                draft = await write_section(
                    section_plan.title,
                    notes=notes,
                    draft_path=sc.output_path,
                    voice_file_paths=self.snapshot.voice_file_paths,
                    feedback_path=feedback_path,
                    section_context=self.build_neighbor_context(
                        plan, section_plan.title
                    ),
                    target_format=self.effective_format,
                    servers=self.research_servers,
                    source_servers=self.source_servers,
                    source_tool_names_list=self.source_tool_names,
                    author_notes=self.author_notes,
                    compute_servers=sc.servers,
                    compute_tool_names=sc.tool_names,
                    glossary_servers=glossary_servers,
                    glossary_tool_names=glossary_tool_names,
                    trace_logger=self.trace_logger,
                    cost_accumulator=self.cost_accumulator,
                )
            draft_path.write_text(draft.content, encoding="utf-8")
            return draft

        all_plans = [task.section_plan for task in queue.rewrites] + queue.additions
        all_coros = [rewrite_coro(sp) for sp in all_plans]

        results = await asyncio.gather(*all_coros, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error("Rewrite/add failed: %s", result)
            else:
                result.title = all_plans[i].title
                self.snapshot.section_drafts[all_plans[i].title] = result

    def drop_section(self, section: str) -> None:
        """Forget a section's draft, which is what dropping one leaves."""
        self.snapshot.section_drafts.pop(section, None)

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
    ) -> StandbyWake:
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
            return StandbyWake(
                revision=revision_task.result(), from_sync=sync_task in done
            )
        self.hooks.sync_requested.clear()
        return StandbyWake(revision=None, from_sync=True)

    async def standby_loop(
        self, initial_wait: float = 120.0, quiet_wait: float = 90.0
    ) -> None:
        while True:
            woke = await self.wait_for_revision_or_sync(self.state)
            revision, from_sync = woke.revision, woke.from_sync
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

            await self.announce_stage(
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
            if await notes.has_plan_breaking():
                await self.check_and_maybe_restart()

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
        async with self.stage_compute("standby") as sc:
            output = await rewrite_final(
                notes,
                standby_draft,
                [],
                output_path=sc.output_path,
                voice_file_paths=self.snapshot.voice_file_paths,
                feedback_path=feedback_path,
                target_format=self.effective_format,
                author_notes=self.author_notes,
                source_servers=self.source_servers,
                source_tool_names_list=self.source_tool_names,
                compute_servers=sc.servers,
                compute_tool_names=sc.tool_names,
                trace_logger=self.trace_logger,
                cost_accumulator=self.cost_accumulator,
            )
        standby_output.write_text(output.content, encoding="utf-8")
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
            sections_parent_id = await do_create_tab(self.doc_id, "Sections")
            self.known_tabs["Sections"] = sections_parent_id
        sections_parent_id = self.known_tabs["Sections"]

        async def placed() -> AsyncIterator[SectionTab]:
            """Each section paired with its tab, creating the ones not there yet.

            A tab creation that fails is non-fatal and yields nothing for that
            section, so the run continues against the tabs it did get.
            """
            for i, section in enumerate(plan.sections):
                tab_name = truncate_tab_title(f"§{i + 1} {section.title}")
                tid = tab_id_for(self.known_tabs, tab_name)
                if not tid:
                    async with gdoc_nonfatal(f"create tab '{tab_name}'"):
                        tid = await do_create_tab(
                            self.doc_id, tab_name, parent_tab_id=sections_parent_id
                        )
                        self.known_tabs[tab_name] = tid
                if tid:
                    self.state.add_section(section.title, tid)
                    yield SectionTab(title=section.title, tab_id=tid)

        self.tab_ids = {tab.title: tab.tab_id async for tab in placed()}

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
                self.known_tabs["Review"] = await do_create_tab(self.doc_id, "Review")

            review_text = f"# Review Findings\n\n{len(findings)} total findings\n"
            review_text += "".join(
                f"\n## [{finding.reviewer}] {finding.location}\n\n"
                f"**Severity:** {finding.severity}\n\n"
                f"{finding.issue}\n\n"
                f"**Suggestion:** {finding.suggestion}\n"
                for finding in findings
            )
            critical_specs = [
                CommentSpec(
                    content=(
                        f"[{finding.reviewer.upper()}] {finding.issue}\n\n"
                        f"Suggestion: {finding.suggestion}"
                    ),
                    anchor_text=finding.text_excerpt or None,
                )
                for finding in findings
                if finding.severity == "critical"
            ]

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

    async def update_overview(
        self, active_stage: str | None = None, *, paused: bool = False
    ) -> None:
        async with gdoc_nonfatal("update overview"):
            snap = self.snapshot
            plan = snap.plan
            completed_stage = snap.stage

            parts: list[str] = [f"# {plan.title if plan else 'Inkwell Pipeline'}\n"]

            all_stages = DISPLAY_STAGES
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

            def marker(index: int) -> str:
                if index == active_idx:
                    return "[>]"
                return "[x]" if index <= completed_idx else "[ ]"

            progress = [f"{marker(i)} {s}" for i, s in enumerate(all_stages)]
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

                def section_line(title: str) -> str:
                    if title not in snap.section_drafts:
                        pending = active_stage == "write"
                        return f"- [{'>' if pending else ' '}] {title}"
                    draft = snap.section_drafts[title]
                    if draft.content.startswith("[Section failed"):
                        return f"- [ ] {title}"
                    return f"- [x] {title} ({draft.word_count} words)"

                parts.extend(section_line(s.title) for s in plan.sections)
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

            if paused:
                parts.append(
                    f"\n**⏸ Paused after {completed_stage}.** Review the tabs and "
                    "comment, then resume to continue the pipeline."
                )
            elif active_stage == "revise":
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
    stop_after: str | None = None,
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
        stop_after=stop_after,
    )
    return await runner.run()
