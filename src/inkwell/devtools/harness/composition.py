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
from inkwell.devtools.harness.catalog import WORKFLOW, portable_harness
from inkwell.devtools.harness.content.docs.catalog import DOCUMENTS
from inkwell.devtools.harness.content.settings import project_settings

CONTENT_ROOT = Path(__file__).parent / "content"


def project_content(root: Path) -> ProjectContent:
    """Everything inkwell publishes beside its compiled plugin tree."""
    harness = portable_harness(root=root)
    return ProjectContent(
        harness=harness,
        documents=DOCUMENTS,
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


REPOSITORY_WIDE: list[RepositoryWriter] = [
    write_rule_reference,
    partial(write_workflow, WORKFLOW),
]
"""Every project-owned generated file outside a native runtime tree."""
