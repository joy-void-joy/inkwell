"""The agents one run holds at once, and how each stays reachable while it works.

A stage that writes six sections at once is six agents, and until now it was
six `query()` calls gathered together — each opening a session, taking one
turn, and closing it. Nothing could be said to one of them. That is fine while
the facts hold still, and this pipeline's facts do not: the author is reading
the document as it fills in and commenting on it, and a writer that has been
working for four minutes on a section the author has just redirected keeps
going, because there is nothing to tell.

An :class:`~lup.actors.cohort.ActorCohort` is the other shape. Each agent
holds one session across every turn it takes, anything addressed to it lands
in front of its next tool call through a hook it never chooses to check, and
the population — not each caller that fans out over it — owns how many run at
once, which of them are running, and what a close reaches.

Three things follow that the gather could not give:

*The wave is the population's.* :meth:`~lup.actors.cohort.ActorCohort.work_all`
runs one piece of work per address under one cap, and hands each answer back
positionally — the result or the exception, faithfully, so a caller that tells
a park from a failure still can. A gather of our own got the cap wrong (there
was none: every section started at once) and answered differently from the
roster about who was running.

*A round is an attempt, not a new agent.* The rewrite stage runs the writers
again over the same sections. As separate `query()` calls that was a stranger
being handed a draft; as `round=2` on the same address it is the agent that
wrote the first one being told what was wrong with it, reattached to the
conversation that still holds the context.

*The roster is on disk.* Who exists and what each was asked is under the
cohort's root, so a console in another process — the web session manager, a
terminal — resolves the same address the cohort's own tools do, and a resumed
run reattaches rather than opening new conversations.

The ids are derived from durable state — a section's slug, a reviewer's name —
which is what makes that last one true. A minted id would be a different agent
on every resume.
"""

from pathlib import Path

from lup.actors.cohort import ActorCohort, ActorRecipe
from lup.actors.refs import ActorRef
from lup.hooks import LupHooksConfig
from lup.mcp import McpServerEntry
from lup.runtime.factory import SessionFactory
from lup.runtime.selection import SessionAutonomy
from lup.runtime.usage import CostAccumulator
from lup.telemetry.trace import TraceLogger

from inkwell.agent.client import (
    BlockCallback,
    is_interrupt,
    observed_factory,
    provider_factory,
    stage_label,
)

COHORT_DIR = "cohort"
"""Where a run keeps its population, beneath the run's own artifacts."""


def stage_recipe(
    *,
    prefix: str,
    model: str | None = None,
    system_prompt: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    autonomy: SessionAutonomy | None = None,
    mcp_servers: dict[str, McpServerEntry] | None = None,
    max_thinking_tokens: int | None = None,
    trace_logger: TraceLogger | None = None,
    cost_accumulator: CostAccumulator | None = None,
    block_callback: BlockCallback | None = None,
) -> ActorRecipe:
    """How one stage's agent opens its session, given the hooks that reach it.

    The same knobs :func:`~inkwell.agent.client.query` takes, wired the same
    way — the difference is that the session is held rather than closed after
    a turn, and that the inbox hook is in the options it opened with. The
    hooks are handed in rather than fetched, because delivery depends on their
    being there: a recipe that had to remember to go and get them is one that
    can be written without them, producing an agent that looks spawned and
    reads nothing anyone sends it.

    The block callback is passed rather than read from the contextvar
    `query()` falls back to, because a recipe runs when the agent opens rather
    than where the caller stood, and ambient state read at that moment belongs
    to whichever task the cap admitted first.

    The label is bound per agent rather than read from ambient state, which is
    what lets two sections write at once and still bill and trace apart.
    """

    def recipe(actor: ActorRef, hooks: LupHooksConfig) -> SessionFactory:
        return observed_factory(
            provider_factory(
                model=model,
                system_prompt=system_prompt,
                tools=tools,
                allowed_tools=allowed_tools,
                autonomy=autonomy,
                tool_servers=mcp_servers,
                max_thinking_tokens=max_thinking_tokens,
                hooks=hooks,
            ),
            label=stage_label(prefix),
            prefix=prefix,
            trace_logger=trace_logger,
            cost_accumulator=cost_accumulator,
            block_callback=block_callback,
        )

    return recipe


def suspended(error: BaseException) -> bool:
    """Whether this failure stopped an agent's work without finishing it.

    Inkwell's three resumable stops. An interrupt — asked for, or a SIGINT
    that killed the provider from outside — leaves the snapshots intact and
    the run resumable. A stop requested at a configured stage boundary has
    already saved its snapshot and expects a later resume to carry on. Neither
    is an agent that failed.

    Recorded finished instead, the resume opens a fresh conversation rather
    than reattaching to the one holding the context, and every door reads a
    waiting agent as a stopped one. Which failures suspend is a fact about
    this application's vocabulary, which is why the cohort takes it rather
    than guessing.
    """
    # Imported where it is used because pipeline imports this module: the
    # exceptions are declared beside the stages that raise them, and a
    # module-level import would close the loop.
    from inkwell.agent.pipeline import PipelineInterrupted, PipelineStopRequested

    if isinstance(error, PipelineStopRequested | PipelineInterrupted):
        return True
    return is_interrupt(error)


def run_cohort(artifacts_dir: Path, parallel: int | None = None) -> ActorCohort:
    """The population one pipeline run holds, rooted under its own artifacts.

    Rooted there rather than under a directory of this layer's choosing so a
    resumed run looks where the last one wrote: the run directory is what
    survives an interruption, and the sessions have to be found beside it.

    ``parallel`` caps the whole population rather than each wave. An agent
    still waiting on the cap has opened no session and been recorded nowhere,
    so an interruption leaves it exactly as the stage found it — which is what
    makes a cut wave resumable rather than half-spent.
    """
    return ActorCohort(
        artifacts_dir / COHORT_DIR,
        parallel=parallel,
        settles=lambda error: not suspended(error),
    )
