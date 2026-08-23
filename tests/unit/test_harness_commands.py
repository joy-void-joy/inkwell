"""The native launch commands exposed by inkwell's harness."""

from pathlib import Path

from typer.testing import CliRunner

from lup.workspace.paths import project_root
from inkwell.devtools.harness.app import app
from inkwell.devtools.harness.catalog import NATIVE_RUNTIMES
from inkwell.devtools.harness.composition import TARGETS


def test_every_native_target_has_a_launch_command() -> None:
    """The target roster is the one source the shared app factory reads."""
    assert list(TARGETS.builders) == ["claude", "codex"]
    assert [runtime.runtime_name for runtime in NATIVE_RUNTIMES] == [
        "Claude Code",
        "Codex",
    ]

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for target in TARGETS.builders:
        assert target in result.output


def test_the_codex_target_compiles_its_project_guidance() -> None:
    """Codex's stricter guidance budget is enforced while building the target."""
    composition = TARGETS.resolve("codex", project_root())[0]

    assert Path("AGENTS.md") in {
        artifact.path for artifact in composition.recipe.desired.artifacts
    }
