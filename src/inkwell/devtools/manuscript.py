"""Read a work of many parts, record it, and say what it has left to do.

The pipeline writes one piece. A textbook is a tree of them, and the unit
anybody revises sits three levels below the book — so before anything can run
against the Atlas, there has to be agreement about what its parts are.
``show`` prints that, which is the cheapest way to find out that an import
read a work differently from how its authors organised it.

The rest is the build loop's front door. ``import`` records the work so runs
months apart share one account of it — including the format its parts are
written as, which every run against the work then inherits rather than guessing
at from the one subsection it is shown — and *adopts* it: every part stamped as
built from the text it already holds, so the first useful pass rewrites what
somebody asked about rather than the whole book. ``status`` is the dependency
pass — what is out of date and why. ``request`` is how an author asks for a
part, and ``changed`` is how anything reports what it moved, which is what
reaches the parts that were leaning on it. ``reaches`` asks the same question
in advance: what does touching this commit me to.

``run`` is the loop, and the only command here that spends anything — every
part it picks up is a whole pipeline run, which is why it takes a limit and
answers ``--dry-run`` first. ``questions`` and ``answer`` are the other side
of it: a run that cannot settle something parks its part and asks, and the
part waits, costing nothing, until somebody answers.
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.utils import JSON_OPT, output_json

from inkwell.agent.config import manuscript_store
from inkwell.agent.glossary import (
    DECLARED_VOCABULARY,
    load_chapter_glossary,
)
from inkwell.manuscript.facts import FACT_KINDS, Dependency, ProposedChange
from inkwell.manuscript.graph import (
    consumers,
    missing_root,
    readings,
    sweep,
)
from inkwell.manuscript.ingest import read_manuscript
from inkwell.manuscript.loop import (
    DEFAULT_CONCURRENCY,
    DEFAULT_PASSES,
    run_loop,
    schedulable,
)
from inkwell.manuscript.mailbox import PartAnswer, PartMailbox
from inkwell.agent.stages import unknown_format
from inkwell.manuscript.recording import import_work
from inkwell.manuscript.store import ManuscriptStore
from inkwell.manuscript.tree import (
    DEFAULT_WORK_FORMAT,
    Manuscript,
    ManuscriptNode,
)
from inkwell.manuscript.vocabulary import (
    Abbreviation,
)

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


def refuse_unrooted(work: str, tree: Manuscript) -> None:
    """Stop with one line about the checkout, where the checkout is gone.

    Every command that reads a work's prose leads with this. The alternative is
    each of them reporting two hundred parts whose text was edited, which is
    both wrong and unactionable — the edit nobody made is a directory nobody
    has.
    """
    gone = missing_root(tree)
    if gone is None:
        return
    typer.echo(f"{work} was imported from {gone}, which is not there.", err=True)
    typer.echo(
        "Restore that checkout, or re-import the work from where it lives now:",
        err=True,
    )
    typer.echo(f"  lup-devtools manuscript import <chapters> --work {work}", err=True)
    raise typer.Exit(1)


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
    target_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="What every part of this work is written as — every run against "
            "it inherits this",
        ),
    ] = DEFAULT_WORK_FORMAT,
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
    refusal = unknown_format(target_format)
    if refusal:
        typer.echo(refusal, err=True)
        raise typer.Exit(1)
    store = manuscript_store()
    recorded = import_work(
        store,
        work,
        chapters,
        title=title,
        target_format=target_format,
        vocabulary=vocabulary,
        adopt=adopt,
    )
    typer.echo(f"{work}: {recorded.parts} leaf part(s) recorded from {chapters}")
    typer.echo(f"  format: {recorded.target_format} — every run inherits it")
    typer.echo(f"  vocabulary: {recorded.vocabulary} declared term(s)")
    typer.echo(f"  dependencies: {recorded.dependencies} across the work")
    typer.echo(f"  adopted: {recorded.adopted} part(s) stamped as built")
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
    refuse_unrooted(work, tree)
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


@app.command("clear")
def clear_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    key: Annotated[str, typer.Argument(help="Which part to return to idle")],
) -> None:
    """Take back what was said about one part, returning it to idle.

    The way out of the two standings that are deliberately sticky. A failed
    part stays failed so a pass cannot pick it up and fail the same way
    forever; a requested part stays requested until something rewrites it. Both
    are right, and both need a door: this is somebody saying they have read the
    failure, or changed their mind, which is the judgement the loop cannot make
    for itself.

    Leaves every stamp and every change fact alone. This says nothing about
    what the part *is* — only that nothing is outstanding on it now, so the
    dependency pass answers for it again on the evidence.
    """
    store = manuscript_store()
    tree = held_work(store, work)
    if tree.node(key) is None:
        typer.echo(f"{work} has no part called {key!r}", err=True)
        raise typer.Exit(1)
    state = store.load_state(work)
    standing = state.standing(key)
    store.publish_state(work, state.declared(key, "idle", ""))
    typer.echo(f"{key}: {standing} → idle")


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


@app.command("run")
def run_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    passes: Annotated[
        int, typer.Option(help="How many passes before stopping and reporting")
    ] = DEFAULT_PASSES,
    limit: Annotated[
        int, typer.Option(help="Most parts to run per pass; 0 for every one")
    ] = 0,
    concurrency: Annotated[
        int, typer.Option(help="How many parts run at once")
    ] = DEFAULT_CONCURRENCY,
    reconcile_wave: Annotated[
        bool,
        typer.Option(
            "--reconcile/--no-reconcile",
            help="Read each wave's rewrites against each other afterwards",
        ),
    ] = True,
    dry_run: Annotated[
        bool, typer.Option(help="Say what would run without spending anything")
    ] = False,
) -> None:
    """Take the work to rest: run what is out of date until nothing is.

    Every part is a full pipeline run, so this spends real money in proportion
    to how much is outstanding — which is what `--dry-run` and `--limit` are
    for, and why the dry run is worth taking first on a work you have not run
    against before.
    """
    store = manuscript_store()
    tree = held_work(store, work)
    refuse_unrooted(work, tree)
    vocabulary = declared_vocabulary(store, work)
    state = store.load_state(work)
    found = sweep(state, readings(tree, vocabulary))
    outstanding = schedulable(found, state)
    stuck = found.blocked()

    if dry_run:
        # The same cut a pass takes, so what the dry run lists is what would
        # actually be picked up rather than everything that qualifies.
        taking = outstanding[:limit] if limit else outstanding
        for verdict in taking:
            typer.echo(verdict.render())
        typer.echo("")
        typer.echo(
            f"{len(taking)} part(s) would run this pass, {passes} pass(es) at most"
            + (f" — {len(outstanding)} outstanding in total" if limit else "")
        )
        for verdict in stuck:
            typer.echo(f"  blocked: {verdict.render()}")
        return
    if not outstanding:
        typer.echo(
            f"{work} is settled — nothing to run"
            if not stuck
            else f"{work} has nothing runnable: {len(stuck)} part(s) have lost "
            "their text, which a run cannot put back"
        )
        for verdict in stuck:
            typer.echo(f"  {verdict.render()}")
        raise typer.Exit(1 if stuck else 0)

    reports = asyncio.run(
        run_loop(
            store,
            work,
            tree,
            vocabulary=vocabulary,
            passes=passes,
            limit=limit,
            concurrency=concurrency,
            reconciling=reconcile_wave,
        )
    )
    for number, report in enumerate(reports, start=1):
        typer.echo(f"pass {number}: {report.render()}")
        for held in report.results:
            typer.echo(
                f"  {held.key}: {held.outcome}{f' — {held.detail}' if held.detail else ''}"
            )
        for conflict in report.conflicts:
            typer.echo(f"  reconciler: {conflict}")
    if reports and reports[-1].settled():
        typer.echo(f"\n{work} is settled")
    else:
        typer.echo(
            f"\n{work} has work outstanding — run again, or answer what is asked"
        )


@app.command("questions")
def questions_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    as_json: JSON_OPT = False,
) -> None:
    """What the work's parts are waiting on, and which part asked each."""
    store = manuscript_store()
    held_work(store, work)
    open_questions = PartMailbox(root=store.work_dir(work)).open()
    if as_json:
        output_json(list(open_questions))
        return
    for question in open_questions:
        addressed = question.addressed_to or work
        typer.echo(f"{question.id}\n  {question.asker} asks {addressed}:")
        typer.echo(f"  {question.prompt}")
    typer.echo("")
    typer.echo(f"{len(open_questions)} question(s) waiting")


@app.command("answer")
def answer_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    question: Annotated[str, typer.Argument(help="Which question, by its id")],
    value: Annotated[str, typer.Argument(help="The answer")],
    answered_by: Annotated[str, typer.Option(help="Who is answering")] = "",
) -> None:
    """Answer a parked question, which is what lets its part run again."""
    store = manuscript_store()
    held_work(store, work)
    mailbox = PartMailbox(root=store.work_dir(work))
    asked = next((held for held in mailbox.open() if held.id == question), None)
    if asked is None:
        typer.echo(f"No question of {work} is waiting under {question!r}", err=True)
        raise typer.Exit(1)
    if not mailbox.answer(
        PartAnswer(id=question, value=value, answered_by=answered_by)
    ):
        typer.echo(f"{question} was already answered", err=True)
        raise typer.Exit(1)
    remaining = mailbox.asked_by(asked.asker)
    state = store.load_state(work)
    if remaining:
        typer.echo(
            f"{asked.asker} answered, and stays parked on "
            f"{len(remaining)} more question(s)"
        )
    else:
        store.publish_state(
            work,
            state.declared(asked.asker, "requested", f"answered: {value}"),
        )
        typer.echo(f"{asked.asker} answered and is ready to run again")


@app.command("reaches")
def reaches_cmd(
    work: Annotated[str, typer.Argument(help="The recorded work")],
    subject: Annotated[str, typer.Argument(help="The term, claim, or part key")],
    kind: Annotated[
        str,
        typer.Option(help=f"Which way parts depend on it: {', '.join(FACT_KINDS)}"),
    ] = "term",
) -> None:
    """Which parts would go out of date if this changed.

    Asked before making a change rather than after, which is the question an
    author actually has: what does touching this definition commit me to.
    """
    store = manuscript_store()
    tree = held_work(store, work)
    if kind not in FACT_KINDS:
        typer.echo(
            f"{kind!r} is no kind of dependency: {', '.join(FACT_KINDS)}", err=True
        )
        raise typer.Exit(1)
    held = readings(tree, declared_vocabulary(store, work))
    leaning = consumers(held, Dependency(kind=kind, subject=subject))
    for node in leaning:
        typer.echo(f"{node.key:<24} {node.title}")
    typer.echo("")
    typer.echo(f"{len(leaning)} part(s) lean on {kind} {subject!r}")
