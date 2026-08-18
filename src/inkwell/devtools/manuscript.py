"""Read a work of many parts and show the tree a run would be about.

The pipeline writes one piece. A textbook is a tree of them, and the unit
anybody revises sits three levels below the book — so before anything can run
against the Atlas, there has to be agreement about what its parts are. This
prints that, which is the cheapest way to find out that an import read a work
differently from how its authors organised it.
"""

import logging
from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.utils import JSON_OPT, output_json

from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.tree import Manuscript, ManuscriptNode

logger = logging.getLogger(__name__)

app = typer.Typer(help="Read a work of many parts into the tree a run works on")

INDENT = "  "
"""One level of nesting in the printed tree."""


def rendered(node: ManuscriptNode, depth: int = 0) -> list[str]:
    """One node and everything under it, as a reader would scan it."""
    where = f"  [{node.path}]" if node.path and not node.children else ""
    return [
        f"{INDENT * depth}{node.key:<18} {node.kind:<11} {node.title}{where}",
        *[line for child in node.children for line in rendered(child, depth + 1)],
    ]


def summary(work: Manuscript) -> list[str]:
    """What the work turned out to hold, counted by what each part is."""
    kinds = [node.kind for node in work.walk()]
    counted = [(kind, kinds.count(kind)) for kind in dict.fromkeys(kinds)]
    leaves = list(work.leaves())
    return [
        "",
        f"{work.title}: " + ", ".join(f"{count} {kind}" for kind, count in counted),
        f"{len(leaves)} leaf part(s) — what a run can be about",
    ]


@app.command("show")
def show_cmd(
    chapters: Annotated[
        Path, typer.Argument(help="Directory holding the work's chapters")
    ],
    title: Annotated[str, typer.Option(help="What to call the work")] = "",
    as_json: JSON_OPT = False,
) -> None:
    """Read a work and print its tree, without running anything against it."""
    if not chapters.is_dir():
        typer.echo(f"No such directory: {chapters}", err=True)
        raise typer.Exit(1)
    work = read_manuscript(chapters, title=title)
    if as_json:
        output_json(work)
        return
    for child in work.children:
        typer.echo("\n".join(rendered(child)))
    typer.echo("\n".join(summary(work)))
