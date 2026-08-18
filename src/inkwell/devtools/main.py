"""Root CLI app composing all devtools sub-apps.

All development tooling is exposed as the ``lup-devtools`` entry point.
Each sub-app groups related commands.

Examples::

    $ uv run lup-devtools --help
    $ uv run lup-devtools agent inspect --json
    $ uv run lup-devtools py info requests
    $ uv run lup-devtools trace show <session_id>
    $ uv run lup-devtools feedback status
    $ uv run lup-devtools feedback ingest-status
    $ uv run lup-devtools dev branches
    $ uv run lup-devtools dev worktree create feat-name
    $ uv run lup-devtools dev check --no-test
    $ uv run lup-devtools version
    $ uv run lup-devtools sync status
    $ uv run lup-devtools usage claude --no-detail
"""

from pathlib import Path

import typer

import inkwell.agent.stages as stages
import inkwell.devtools.feedback as feedback
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.harness.profile_app import create_profile_app
from lup.devtools.subapps import SubApp, compose
from inkwell.devtools.agent import app as agent_app
from inkwell.devtools.corpus import app as corpus_app
from inkwell.devtools.dev.app import app as dev_app
from inkwell.devtools.harness.app import app as harness_app
from inkwell.devtools.manuscript import app as manuscript_app
from inkwell.devtools.profiles import inkwell_profile_directory
from inkwell.devtools.setup import app as setup_app
from inkwell.devtools.subapps import APPLICATION_SPECS, INHERITED

# Mounted from the composition root because the profile origin reads the setup
# module for where a profile directory lives, which leaves setup unable to
# reach back for the tree that curates them.
setup_app.add_typer(create_profile_app(inkwell_profile_directory()), name="profile")


def assembled_prompt() -> AgentPrompt:
    """This application's stage prompts, as the health report weighs them.

    inkwell has no single system prompt: a run is a pipeline, and each stage
    opens its session with one of these. The report weighs what sessions
    actually receive, so all of them are what it is given — read off the
    module rather than matched by shape, so renaming the package moves it.
    """
    named = {
        name: value
        for name, value in vars(stages).items()
        if name.isupper() and isinstance(value, str)
    }
    source = stages.__file__
    return AgentPrompt(
        sections=sorted(named),
        rendered="\n\n".join(named[name] for name in sorted(named)),
        source=None if source is None else Path(source),
    )


APPLICATION_APPS = {
    "agent": agent_app,
    "corpus": corpus_app,
    "dev": dev_app,
    "feedback": feedback.create_app(assembled_prompt),
    "harness": harness_app,
    "manuscript": manuscript_app,
    "setup": setup_app,
}
"""Where each application spec meets the Typer app answering to its name.

This module is the composition root and nothing imports it, which is what
lets the apps be named here without the guidance that documents them coming
along. A spec with no app raises on the first invocation rather than serving
a CLI missing a command the docs promise.
"""

app = typer.Typer(
    help="lup-devtools: development and analysis tools",
    pretty_exceptions_show_locals=False,
    no_args_is_help=True,
)

compose(
    app,
    sorted(
        [
            *INHERITED,
            *[
                SubApp(spec=spec, app=APPLICATION_APPS[spec.name])
                for spec in APPLICATION_SPECS
            ],
        ],
        key=lambda entry: entry.spec.name,
    ),
)
