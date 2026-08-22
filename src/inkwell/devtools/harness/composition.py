"""What inkwell publishes through its native target, and what writes it.

The builders and the selector are the library's; named here is only what is
inkwell's own — the content its harness compiles beside, and the generated
files that belong to no native tree at all.
"""

from functools import partial
from pathlib import Path

from lup.devtools.dev.rules import write_rule_reference
from lup.devtools.dev.workflow import write_workflow
from lup.devtools.harness.composition import NativeTargets, claude_composition
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.harness.generate import NativeHarnessComposition, ProjectContent
from lup.harness.banner import GeneratedBanner
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.workspace.paths import project_root
from inkwell.devtools.harness.catalog import (
    WORKFLOW,
    declared_hook_set,
    portable_harness,
)
from inkwell.devtools.harness.content.docs.catalog import documents
from inkwell.devtools.harness.content.settings import project_settings
from inkwell.environment.cli.compile import render_entry_point_commands

CONTENT_ROOT = Path(__file__).parent / "content"


def project_content(root: Path) -> ProjectContent:
    """Everything inkwell publishes beside its compiled plugin tree."""
    harness = portable_harness(root=root)
    return ProjectContent(
        harness=harness,
        documents=documents(),
        assets=[CONTENT_ROOT / "assets" / "file_suggest.sh"],
        settings=project_settings(harness.plugins[0]),
    )


def claude_target(root: Path) -> NativeHarnessComposition:
    """Inkwell's content, compiled through the Claude adapter.

    No installer guidance: that third argument is the document a *template*
    hands a target it is installed into, and inkwell is nobody's starting
    point.
    """
    return claude_composition(root, project_content(root))


TARGETS = NativeTargets(builders={"claude": claude_target})
"""Every native runtime inkwell generates a tree for, by CLI selector."""


COMMANDS_PATH = Path("src/inkwell/environment/cli/commands.py")
"""Where the compiled typer commands live, beside the CLI that registers them."""

COMMANDS_COMMAND = "uv run lup-devtools harness generate all"
"""The command a reader runs to rebuild the compiled typer commands."""


def entry_point_commands_artifact() -> Artifact:
    """The compiled entry point commands, gated like any generated file."""
    return Artifact.generated(
        path=COMMANDS_PATH,
        body=render_entry_point_commands(),
        semantic_id="inkwell.cli.commands",
        banner=GeneratedBanner(
            source="inkwell.environment.cli.compile", command=COMMANDS_COMMAND
        ),
    )


def write_entry_point_commands(
    root: Path | None = None, *, check: bool = False
) -> Path:
    """Write or verify the typer commands compiled from the entry point declaration."""
    return write_generated_file(
        entry_point_commands_artifact(),
        root or project_root(),
        COMMANDS_COMMAND,
        check=check,
    )


REPOSITORY_WIDE: list[RepositoryWriter] = [
    partial(write_rule_reference, selection=declared_hook_set().rules),
    partial(write_workflow, WORKFLOW),
    write_entry_point_commands,
]
"""Every project-owned generated file outside a native runtime tree."""
