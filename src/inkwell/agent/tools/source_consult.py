"""Source-document access tools — locate and read the authoritative inputs.

The pipeline's plan and research notes are lossy summaries of the source
documents. These tools give every stage direct access to the originals,
so claims get verified by reading rather than reconstructed from memory.

A document is read whole, by a reader agent holding it in context through
the runtime's own Read. Nothing here pulls text out of a PDF: an extractor
returns an empty string from a scanned or image-only page, and an empty
string reads as a document that does not say the thing rather than as an
extraction that failed. Locating and answering are therefore the same
operation — ``consult_source`` finds the pages and quotes them — because
the cheap locate step that used to precede it could only exist on top of
an extracted layer that lies exactly where it is least noticed.

``pdfinfo`` (poppler-utils) supplies the page count, which is metadata
rather than content: it reports the same number for a scanned document as
for a born-digital one, so nothing downstream reads a silence into it.
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from email import message_from_string
from pathlib import Path
from typing import Literal

import sh
from pydantic import BaseModel, Field

from inkwell.agent.config import stage_model
from inkwell.agent.client import query, result_text
from lup.runtime.usage import CostAccumulator
from lup.mcp import LupMcpTool, ToolError, lup_tool
from lup.telemetry.trace import TraceLogger

logger = logging.getLogger(__name__)

REGISTRY_FILENAME = "sources.json"


class SourceDocument(BaseModel):
    """One registered source document."""

    label: str = Field(description="Short identifier (file stem)")
    path: str = Field(description="Absolute path to the original document")
    kind: Literal["pdf", "text"] = Field(description="Document type")
    page_count: int = Field(default=0, description="Number of pages (PDFs)")


def registry_path_for(artifacts_dir: Path) -> Path:
    return artifacts_dir / REGISTRY_FILENAME


def pdf_page_count(pdf_path: Path) -> int:
    """How many pages a PDF has, read from its metadata by ``pdfinfo``.

    A count is not content: poppler reports it from the page tree, so a
    scanned document answers the same as a born-digital one and a reader
    handed the number is never told a silence it should have questioned.

    ``pdfinfo`` emits ``Key: value`` lines, which is the header grammar
    :mod:`email` already parses — the labels carry spaces and the values
    carry colons, so reading them by hand would be re-deriving a parser
    that ships with the language.
    """
    report = message_from_string(str(sh.Command("pdfinfo")(str(pdf_path))))
    pages = report["Pages"]
    if pages is None:
        raise ValueError(f"pdfinfo reported no page count for {pdf_path}")
    return int(pages)


def build_source_registry(
    source_paths: list[str], artifacts_dir: Path
) -> list[SourceDocument]:
    """Register source documents and build their navigation text layers.

    Skips paths that don't exist as local files. Persists the registry
    to ``artifacts_dir/sources.json`` so tools in any later stage (or a
    resumed process) can load it.
    """
    sources_dir = artifacts_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    documents: list[SourceDocument] = []
    for raw in source_paths:
        original = Path(raw).expanduser()
        if not original.is_file():
            continue
        path = sources_dir / original.name
        if path.resolve() != original.resolve():
            shutil.copy2(original, path)
        label = path.stem
        if path.suffix.lower() == ".pdf":
            try:
                page_count = pdf_page_count(path)
            except (sh.ErrorReturnCode, sh.CommandNotFound, OSError, ValueError):
                logger.warning(
                    "pdfinfo could not count the pages of %s; registering it "
                    "without page windows, so readers take it whole",
                    path,
                    exc_info=True,
                )
                page_count = 0
            documents.append(
                SourceDocument(
                    label=label,
                    path=str(path.resolve()),
                    kind="pdf",
                    page_count=page_count,
                )
            )
        else:
            documents.append(
                SourceDocument(
                    label=label,
                    path=str(path.resolve()),
                    kind="text",
                )
            )

    registry = registry_path_for(artifacts_dir)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps([d.model_dump() for d in documents], indent=2),
        encoding="utf-8",
    )
    return documents


async def build_source_registry_async(
    source_paths: list[str], artifacts_dir: Path
) -> list[SourceDocument]:
    """Thread-offloaded ``build_source_registry`` (it copies files and shells out)."""
    return await asyncio.to_thread(build_source_registry, source_paths, artifacts_dir)


def load_source_registry(registry: Path) -> list[SourceDocument]:
    if not registry.exists():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        return [SourceDocument.model_validate(item) for item in data]
    except (json.JSONDecodeError, ValueError):
        logger.warning("Unreadable source registry at %s", registry)
        return []


SOURCE_READER_PROMPT = """\
You answer one focused question about a source document by reading the
document itself.

The Read tool accepts pages='N-M' for PDFs (at most 20 pages per call).
Use the page hints in your task; if the answer isn't there, widen the
range or jump to where the document's structure says it should be
(table of contents, index, chapter headings).

Your final message IS the answer that gets returned to the caller:
1. Verbatim quotes from the document, each with its page number
2. A brief synthesis after the quotes

Never paraphrase where a quote will do — the caller needs the
document's exact words, not your summary of them. If the document does
not answer the question, say exactly that; do not fill the gap from
general knowledge.

Where the question is a "where" — which pages define this symbol, state
this theorem, set this convention — the page numbers are the answer, so
give every page you found it discussed on, not only the first."""


class ConsultSourceInput(BaseModel):
    question: str = Field(
        description="The focused question the reader should answer from the document"
    )
    label: str = Field(
        default="",
        description="Which document to consult (required when several are registered)",
    )
    pages: str = Field(
        default="",
        description=(
            "Page range hint like '105-124' — usually page numbers an earlier "
            "answer cited. The reader starts there and widens if needed."
        ),
    )


class ConsultSourceOutput(BaseModel):
    answer: str = Field(description="Verbatim quotes with page numbers + synthesis")
    label: str = Field(description="Document consulted")


class ListSourceDocumentsInput(BaseModel):
    pass


class ListSourceDocumentsOutput(BaseModel):
    documents: list[SourceDocument] = Field(description="Registered source documents")


def make_source_consult_tools(
    registry_provider: Callable[[], Path],
    *,
    reader_model: str | None = None,
) -> list[LupMcpTool]:
    """Build the source-document tools bound to a session's registry.

    ``registry_provider`` is called lazily at tool-call time so the
    tools can be constructed before the session's notes directory exists.
    """

    def load_registry() -> list[SourceDocument]:
        documents = load_source_registry(registry_provider())
        if not documents:
            raise ToolError(
                "No source documents registered for this session. "
                "The piece may have purely web/text sources; use the research "
                "findings and fetch tools instead."
            )
        return documents

    def resolve(label: str) -> SourceDocument:
        documents = load_registry()
        if label:
            for doc in documents:
                if doc.label == label:
                    return doc
            known = ", ".join(d.label for d in documents)
            raise ToolError(f"No source document labeled {label!r}. Known: {known}")
        if len(documents) == 1:
            return documents[0]
        known = ", ".join(d.label for d in documents)
        raise ToolError(f"Several documents are registered ({known}); pass label.")

    @lup_tool(
        "Ask a focused question of a source document. A nested reader agent "
        "opens the document itself and answers with verbatim quotes and page "
        "citations. Use this whenever a claim, definition, theorem statement, "
        "notation, or convention must come from the source itself — plan key "
        "points and research summaries are lossy, and external papers may use "
        "opposite conventions. This is also how you LOCATE something: ask "
        "where the document defines a symbol or states a theorem and the "
        "reader navigates by the document's own structure (contents, index, "
        "headings), returning the pages along with the answer. Pass `pages` "
        "when you already know roughly where to look.",
        name="consult_source",
    )
    async def consult_source(inp: ConsultSourceInput) -> ConsultSourceOutput:
        doc = resolve(inp.label)
        page_note = ""
        if doc.kind == "pdf":
            page_note = f" The document has {doc.page_count} pages."
            if inp.pages:
                page_note += f" Start with pages {inp.pages}."
        task = (
            f"Document: {doc.path}{page_note}\n\n"
            f"Question: {inp.question}\n\n"
            f"Read the document and answer with verbatim quotes and page numbers."
        )
        collector = await query(
            task,
            model=reader_model or stage_model("reader"),
            system_prompt=SOURCE_READER_PROMPT,
            tools=["Read"],
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
            prefix=f"[consult:{doc.label}] ",
        )
        answer = result_text(collector)
        if not answer:
            raise ToolError(
                "The reader produced no answer; retry with a narrower question "
                "or an explicit page range."
            )
        return ConsultSourceOutput(answer=answer, label=doc.label)

    @lup_tool(
        "List the source documents registered for this session, with labels, "
        "kinds, and page counts. Use the labels with consult_source. A page "
        "count of 0 on a PDF means poppler could not read one, so ask for the "
        "document whole rather than by page range.",
        name="list_source_documents",
    )
    async def list_source_documents(
        _inp: ListSourceDocumentsInput,
    ) -> ListSourceDocumentsOutput:
        return ListSourceDocumentsOutput(documents=load_registry())

    return [consult_source, list_source_documents]


READING_NOTES_DIRNAME = "source_notes"

READING_NOTES_PROMPT = """\
You write reading notes for one window of a source document. The notes
are the durable memory other agents rely on when they cannot hold the
whole document in context — they must stand alone.

Read your assigned pages with the Read tool (it accepts pages='N-M',
at most 20 pages per call — split larger windows into two calls), then
write notes containing:

- The section/chapter structure inside the window, with page numbers
- Every formal definition VERBATIM, with its page number
- Every theorem, proposition, and algorithm statement verbatim, with page
- The conventions that govern reading the document — notation, symbol
  meanings, encodings, digit order, index origins — quoted verbatim
- A short prose summary of what the window argues

Quote; don't paraphrase. A summarized definition poisons every
downstream stage that trusts these notes. Write the notes to the output
path given in your task using the Write tool."""


def reading_notes_dir(artifacts_dir: Path) -> Path:
    return artifacts_dir / READING_NOTES_DIRNAME


async def build_reading_notes(
    doc: SourceDocument,
    artifacts_dir: Path,
    *,
    window: int = 24,
    concurrency: int = 3,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
) -> list[Path]:
    """Build per-window reading notes for a PDF source document.

    Parallel nested readers each cover ``window`` pages and persist
    verbatim-quoting notes to disk, so no later stage depends on holding
    the whole document in one context (and compaction loses nothing).
    """
    notes_dir = reading_notes_dir(artifacts_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)

    async def read_window(start: int, end: int) -> Path:
        out_path = notes_dir / f"{doc.label}_p{start:04d}-{end:04d}.md"
        if out_path.exists():
            return out_path
        task = (
            f"Document: {doc.path}\n"
            f"Assigned pages: {start}-{end} (document has {doc.page_count}).\n\n"
            f"Write the reading notes to: {out_path}"
        )
        async with semaphore:
            await query(
                task,
                model=stage_model("reader"),
                system_prompt=READING_NOTES_PROMPT,
                tools=["Read", "Write"],
                max_thinking_tokens=128_000 - 1,
                autonomy="unattended",
                prefix=f"[reading:{doc.label}:{start}-{end}] ",
                trace_logger=trace_logger,
                cost_accumulator=cost_accumulator,
            )
        if not out_path.exists():
            logger.warning(
                "Reading notes missing for %s pages %d-%d", doc.label, start, end
            )
        return out_path

    windows = [
        (start, min(start + window - 1, doc.page_count))
        for start in range(1, doc.page_count + 1, window)
    ]
    results = await asyncio.gather(*(read_window(a, b) for a, b in windows))
    return [p for p in results if p.exists()]
