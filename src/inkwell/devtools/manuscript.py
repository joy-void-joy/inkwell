"""Read a work of many parts, record it, and say what it has left to do.

The pipeline writes one piece. A textbook is a tree of them, and the unit
anybody revises sits three levels below the book — so before anything can run
against the Atlas, there has to be agreement about what its parts are.
``show`` prints that, which is the cheapest way to find out that an import
read a work differently from how its authors organised it.

The rest is the build loop's front door. ``import`` records the work so runs
months apart share one account of it, and *adopts* it: every part stamped as
built from the text it already holds, so the first useful pass rewrites what
somebody asked about rather than the whole book. ``status`` is the dependency
pass — what is out of date and why. ``request`` is how an author asks for a
part, and ``changed`` is how anything reports what it moved, which is what
reaches the parts that were leaning on it.
"""

import logging
from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.utils import JSON_OPT, output_json

from inkwell.agent.config import manuscript_store
from inkwell.agent.glossary import (
    DECLARED_VOCABULARY,
    load_chapter_glossary,
    write_chapter_glossary,
)
from inkwell.manuscript.facts import FACT_KINDS, Dependency, ProposedChange
from inkwell.manuscript.graph import adopted, readings, sweep
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import Manuscript, ManuscriptNode
from inkwell.manuscript.vocabulary import (
    Abbreviation,
    as_glossary,
    read_abbreviations,
)

logger = logging.getLogger(__name__)

app = typer.Typer(help="Read a work of many parts into the tree a run works on")

ABBREVIATIONS_PATH = Path("includes/abbreviations.md")
"""Where an mkdocs work keeps the vocabulary it declares, relative to its docs.

The Atlas's own location. Overridable per import, because it is a convention
of that project rather than a rule of the format.
"""

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


def held_work(store: ManuscriptStore, work: str) -> Manuscript:
    """One recorded work's tree, or a clear refusal where it was never imported."""
    tree = store.load_tree(work)
    if tree is None:
        typer.echo(
            f"No work called {work!r} is recorded. Import it first:\n"
            f"  lup-devtools manuscript import <chapters-dir> --work {work}",
            err=True,
        )
        raise typer.Exit(1)
    return tree


def declared_vocabulary(store: ManuscriptStore, work: str) -> tuple[Abbreviation, ...]:
    """The terms a work's authors declared, as the sweep matches them.

    Read back from what the import wrote rather than from the work's own
    checkout, so a status check needs no source tree to hand.
    """
    held = load_chapter_glossary(store.glossary_path(work, DECLARED_VOCABULARY))
    return tuple(
        Abbreviation(term=entry.term, meaning=entry.meaning) for entry in held.terms
    )


@app.command("import")
def import_cmd(
    chapters: Annotated[
        Path, typer.Argument(help="Directory holding the work's chapters")
    ],
    work: Annotated[str, typer.Option(help="Slug to record this work under")],
    title: Annotated[str, typer.Option(help="What to call the work")] = "",
    vocabulary: Annotated[
        Path | None,
        typer.Option(help="The work's declared abbreviations file"),
    ] = None,
    adopt: Annotated[
        bool,
        typer.Option(help="Stamp every part as built from the text it already holds"),
    ] = True,
) -> None:
    """Record a work so runs months apart share one account of it.

    Adopting is on by default and is the difference between a loop you can use
    tomorrow and one that must first rewrite two hundred parts. Turn it off to
    treat every part as unwritten, which is what a work being drafted from
    nothing actually wants.
    """
    if not chapters.is_dir():
        typer.echo(f"No such directory: {chapters}", err=True)
        raise typer.Exit(1)
    store = manuscript_store()
    tree = read_manuscript(chapters, title=title)
    store.publish_tree(work, tree)

    abbreviations_at = (
        vocabulary if vocabulary is not None else chapters.parent / ABBREVIATIONS_PATH
    )
    abbreviations = read_abbreviations(abbreviations_at)
    if abbreviations:
        vocabulary_path = store.glossary_path(work, DECLARED_VOCABULARY)
        vocabulary_path.parent.mkdir(parents=True, exist_ok=True)
        write_chapter_glossary(vocabulary_path, as_glossary(abbreviations))

    held = readings(tree, abbreviations)
    state = store.load_state(work)
    if adopt:
        state = adopted(state, held)
    store.publish_state(work, state)

    leaves = len(held)
    edges = sum(len(reading.consumed.dependencies) for reading in held)
    typer.echo(f"{work}: {leaves} leaf part(s) recorded from {chapters}")
    typer.echo(f"  vocabulary: {len(abbreviations)} declared term(s)")
    typer.echo(f"  dependencies: {edges} across the work")
    typer.echo(f"  adopted: {len(state.stamps)} part(s) stamped as built")
    typer.echo(f"  recorded at: {store.work_dir(work)}")


@app.command("status")
def status_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    dirty_only: Annotated[
        bool, typer.Option("--dirty", help="Show only what is outstanding")
    ] = False,
    as_json: JSON_OPT = False,
) -> None:
    """What the work has left to do, and why each part of it is outstanding."""
    store = manuscript_store()
    tree = held_work(store, work)
    state = store.load_state(work)
    found = sweep(state, readings(tree, declared_vocabulary(store, work)))
    if as_json:
        output_json(found)
        return
    shown = found.dirty() if dirty_only else found.verdicts
    for verdict in shown:
        typer.echo(verdict.render())
    counts = found.counted()
    typer.echo("")
    typer.echo(", ".join(f"{count} {kind}" for kind, count in counts.items()))
    typer.echo(
        "settled — nothing outstanding"
        if found.settled()
        else f"{len(found.dirty())} part(s) outstanding"
    )


@app.command("request")
def request_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    key: Annotated[str, typer.Argument(help="Which part to ask for")],
    reason: Annotated[str, typer.Option(help="What you want done to it")] = "",
) -> None:
    """Ask for one part to be revised, which is what makes it outstanding."""
    store = manuscript_store()
    tree = held_work(store, work)
    if tree.node(key) is None:
        typer.echo(f"{work} has no part called {key!r}", err=True)
        raise typer.Exit(1)
    state = store.load_state(work).declared(key, "requested", reason)
    store.publish_state(work, state)
    typer.echo(f"{key}: revision asked for{f' — {reason}' if reason else ''}")


@app.command("changed")
def changed_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    key: Annotated[str, typer.Argument(help="The part whose run changed something")],
    subject: Annotated[str, typer.Argument(help="What it changed")],
    kind: Annotated[
        str,
        typer.Option(
            help=f"Which way the other parts depend on it: {', '.join(FACT_KINDS)}"
        ),
    ] = "term",
    detail: Annotated[str, typer.Option(help="What about it changed")] = "",
) -> None:
    """Record what a run changed, so the parts leaning on it hear about it.

    What the node runner calls when it finishes, exposed as a command because
    an author who edits a definition by hand has changed exactly the same
    thing and the parts downstream deserve to know either way.
    """
    store = manuscript_store()
    tree = held_work(store, work)
    if kind not in FACT_KINDS:
        typer.echo(
            f"{kind!r} is no kind of dependency: {', '.join(FACT_KINDS)}", err=True
        )
        raise typer.Exit(1)
    change = ProposedChange(
        dependency=Dependency(kind=kind, subject=subject), detail=detail
    )
    state = store.load_state(work).changed(key, [change])
    store.publish_state(work, state)
    found = sweep(state, readings(tree, declared_vocabulary(store, work)))
    reached = [held for held in found.dirty() if held.staleness == "upstream"]
    typer.echo(f"{key} changed {change.dependency.render()}")
    typer.echo(f"  reached {len(reached)} part(s):")
    for verdict in reached:
        typer.echo(f"    {verdict.key}")
