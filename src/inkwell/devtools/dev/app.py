"""Inkwell's `dev` tree: the library's, plus the one command only it has.

The workflow commands — worktrees, branches, PRs, conflicts, the quality gate,
the harness generation loop — are the library's, composed here over what this
repository declares about itself. Added below is the sandbox image inkwell's
LaTeX and document tools run inside, which no other project on lup builds.
"""

from pathlib import Path

import lup.devtools.dev.check as check
import inkwell.devtools.harness.catalog as catalog
from lup.devtools.dev.app import DevDeclarations, create_dev_app
from inkwell.devtools.harness.composition import REPOSITORY_WIDE, TARGETS
from inkwell.devtools.harness.content.guidance import DOCUMENT as GUIDANCE


def declared() -> DevDeclarations:
    """What inkwell tells the dev tree, read where a command runs.

    One test root: lup is a git dependency rather than a second workspace
    member, so its suite runs in its own repository and this gate exercises
    only what this repository owns.
    """
    return DevDeclarations(
        project=catalog.dev_project(),
        hooks=catalog.declared_hook_set(),
        plugin=catalog.declared_plugin(),
        test_roots=[check.TestRoot(name="pytest", directory=Path.cwd())],
    )


app = create_dev_app(
    declared=declared,
    native_targets=TARGETS,
    repository_writers=REPOSITORY_WIDE,
    guidance=GUIDANCE,
    relocate_roots=[Path("src"), Path("tests")],
)


@app.command("build-sandbox-image")
def build_sandbox_image_cmd() -> None:
    """Build the academic sandbox image (pandoc + tectonic + poppler)."""
    from inkwell.agent.sandbox_image import build_sandbox_image

    build_sandbox_image()
