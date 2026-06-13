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
TEX_TOOLING_INSTALL = (
    "apt-get update -qq && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
    "--no-install-recommends pandoc tectonic"
)


class LatexArtifacts(BaseModel):
    """Paths of produced LaTeX artifacts on the host (empty when skipped)."""

    tex_path: str = Field(default="", description="paper.tex on the host")
    pdf_path: str = Field(default="", description="compiled paper.pdf on the host")
    log: str = Field(default="", description="Tooling output for diagnostics")


def ensure_tex_tooling(sandbox: Sandbox) -> bool:
    """Install pandoc + tectonic in the container if missing."""
    probe = sandbox.run_shell(TEX_TOOLING_PROBE)
    if probe["exit_code"] == 0:
        return True
    logger.info("Installing TeX tooling in sandbox (pandoc, tectonic)")
    install = sandbox.run_shell(TEX_TOOLING_INSTALL)
    if install["exit_code"] != 0:
        logger.warning("TeX tooling install failed: %s", install["stdout"][-500:])
        return False
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
