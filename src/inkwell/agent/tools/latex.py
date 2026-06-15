"""LaTeX deliverable for academic pieces — pandoc conversion + tectonic compile.

For an arXiv-bound paper, markdown in a Google Doc is a review surface,
not the deliverable. This module turns the final markdown into
``paper.tex`` (pandoc) and a compiled ``paper.pdf`` (tectonic), both
produced inside the session sandbox so the host needs no TeX install.
"""

import asyncio
import logging
import shlex
from pathlib import Path

from pydantic import BaseModel, Field

from lup.sandbox import Sandbox

logger = logging.getLogger(__name__)

TEX_TOOLING_PROBE = "command -v pandoc >/dev/null && command -v tectonic >/dev/null"


class LatexArtifacts(BaseModel):
    """Paths of produced LaTeX artifacts on the host (empty when skipped)."""

    tex_path: str = Field(default="", description="paper.tex on the host")
    pdf_path: str = Field(default="", description="compiled paper.pdf on the host")
    log: str = Field(default="", description="Tooling output for diagnostics")


def ensure_tex_tooling(sandbox: Sandbox) -> bool:
    """Probe for pandoc + tectonic, provisioned in the sandbox image."""
    return sandbox.run_shell(TEX_TOOLING_PROBE)["exit_code"] == 0


def build_latex_artifacts_sync(
    markdown_text: str,
    sandbox: Sandbox,
    *,
    title: str,
) -> LatexArtifacts:
    """Convert markdown to paper.tex and compile paper.pdf in the sandbox.

    Best-effort: returns whatever artifacts were produced; a missing
    ``pdf_path`` with a populated ``log`` means compilation failed and
    the .tex (when present) is still shippable.
    """
    shared = sandbox.shared_dir
    (shared / "paper.md").write_text(markdown_text, encoding="utf-8")

    if not ensure_tex_tooling(sandbox):
        return LatexArtifacts(log="TeX tooling unavailable in sandbox")

    log_parts: list[str] = []
    pandoc = sandbox.run_shell(
        "cd /shared && pandoc paper.md -s "
        f"--metadata title={shlex.quote(title)} -o paper.tex"
    )
    log_parts.append(pandoc["stdout"])
    tex_host = shared / "paper.tex"
    if pandoc["exit_code"] != 0 or not tex_host.exists():
        return LatexArtifacts(log="pandoc failed:\n" + "\n".join(log_parts))

    tectonic = sandbox.run_shell("cd /shared && tectonic paper.tex")
    log_parts.append(tectonic["stdout"])
    pdf_host = shared / "paper.pdf"
    if tectonic["exit_code"] != 0 or not pdf_host.exists():
        return LatexArtifacts(
            tex_path=str(tex_host),
            log="tectonic failed:\n" + "\n".join(log_parts),
        )

    return LatexArtifacts(
        tex_path=str(tex_host),
        pdf_path=str(pdf_host),
        log="\n".join(log_parts),
    )


async def build_latex_artifacts(
    markdown_text: str,
    sandbox: Sandbox,
    *,
    title: str,
) -> LatexArtifacts:
    """Thread-offloaded ``build_latex_artifacts_sync`` (compile is blocking)."""
    return await asyncio.to_thread(
        build_latex_artifacts_sync, markdown_text, sandbox, title=title
    )


def latex_artifacts_note(artifacts: LatexArtifacts, links: list[str]) -> str:
    """Render a short markdown block linking the produced artifacts."""
    if not artifacts.tex_path and not artifacts.pdf_path:
        return ""
    lines = ["", "---", "", "**LaTeX artifacts**", ""]
    lines.extend(f"- {link}" for link in links)
    return "\n".join(lines) + "\n"


def save_artifacts_to(artifacts: LatexArtifacts, target_dir: Path) -> LatexArtifacts:
    """Copy produced artifacts into the session's artifacts directory."""
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = artifacts.model_copy()
    for attr in ("tex_path", "pdf_path"):
        value = getattr(artifacts, attr)
        if not value:
            continue
        source = Path(value)
        if source.exists():
            destination = target_dir / source.name
            destination.write_bytes(source.read_bytes())
            setattr(copied, attr, str(destination))
    return copied


LATEX_PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage{amsmath,amssymb,amsthm}
\usepackage[margin=1in]{geometry}
\usepackage{hyperref}
\newtheorem{theorem}{Theorem}
\newtheorem{lemma}{Lemma}
\newtheorem{proposition}{Proposition}
\newtheorem{corollary}{Corollary}
\theoremstyle{definition}
\newtheorem{definition}{Definition}
"""


LATEX_ESCAPES = {
    "\\": "\\textbackslash{}",
    "&": "\\&",
    "%": "\\%",
    "$": "\\$",
    "#": "\\#",
    "_": "\\_",
    "{": "\\{",
    "}": "\\}",
    "~": "\\textasciitilde{}",
    "^": "\\textasciicircum{}",
}
LATEX_ESCAPE_TABLE = str.maketrans(LATEX_ESCAPES)


def escape_latex(text: str) -> str:
    """Escape the LaTeX special characters that appear in a plain title.

    Single pass via ``str.translate`` so the braces a replacement introduces
    (e.g. ``\\textbackslash{}``) are not themselves re-escaped.
    """
    return text.translate(LATEX_ESCAPE_TABLE)


def assemble_latex_document(body: str, title: str) -> str:
    """Wrap LaTeX body content (sections, environments) in a compilable paper.

    The academic writers emit body content only; this adds the preamble,
    title, and document environment so the result compiles as a standalone
    paper. If the body already carries a preamble it is returned unchanged.
    """
    if "\\documentclass" in body:
        return body
    title_cmd = ""
    if title.strip():
        title_cmd = "\\title{" + escape_latex(title) + "}\n\\maketitle\n\n"
    return (
        LATEX_PREAMBLE
        + "\\begin{document}\n"
        + title_cmd
        + body
        + "\n\\end{document}\n"
    )


def compile_tex_sync(tex_source: str, sandbox: Sandbox) -> LatexArtifacts:
    """Write paper.tex and compile paper.pdf with tectonic (no pandoc).

    For LaTeX-authored content the source is already a document, so this skips
    the markdown conversion. Best-effort: a missing ``pdf_path`` with a log
    means compilation failed and the .tex is still shippable.
    """
    shared = sandbox.shared_dir
    tex_host = shared / "paper.tex"
    tex_host.write_text(tex_source, encoding="utf-8")

    if not ensure_tex_tooling(sandbox):
        return LatexArtifacts(
            tex_path=str(tex_host), log="TeX tooling unavailable in sandbox"
        )

    tectonic = sandbox.run_shell("cd /shared && tectonic paper.tex")
    pdf_host = shared / "paper.pdf"
    if tectonic["exit_code"] != 0 or not pdf_host.exists():
        return LatexArtifacts(
            tex_path=str(tex_host), log="tectonic failed:\n" + tectonic["stdout"]
        )
    return LatexArtifacts(
        tex_path=str(tex_host), pdf_path=str(pdf_host), log=tectonic["stdout"]
    )


async def compile_tex(tex_source: str, sandbox: Sandbox) -> LatexArtifacts:
    """Thread-offloaded ``compile_tex_sync`` (compile is blocking)."""
    return await asyncio.to_thread(compile_tex_sync, tex_source, sandbox)


def render_pdf_preview_sync(sandbox: Sandbox, *, max_pages: int = 20) -> list[Path]:
    """Rasterize paper.pdf to per-page PNGs in /shared via pdftoppm.

    Returns the host paths of the page images (empty if there is no PDF or
    pdftoppm fails) for insertion into the rendered Preview tab.
    """
    shared = sandbox.shared_dir
    if not (shared / "paper.pdf").exists():
        return []
    result = sandbox.run_shell(
        "cd /shared && rm -f preview-*.png && "
        f"pdftoppm -png -r 150 -l {max_pages} paper.pdf preview"
    )
    if result["exit_code"] != 0:
        logger.warning("pdftoppm failed: %s", result["stdout"][-300:])
        return []
    return sorted(shared.glob("preview-*.png"))


async def render_pdf_preview(sandbox: Sandbox, *, max_pages: int = 20) -> list[Path]:
    """Thread-offloaded ``render_pdf_preview_sync`` (rasterization is blocking)."""
    return await asyncio.to_thread(
        render_pdf_preview_sync, sandbox, max_pages=max_pages
    )
