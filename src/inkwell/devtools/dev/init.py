"""Package renaming for downstream project initialization.

Renames the ``lup_template`` Python package to a project-specific name,
updating imports, entry points, and CLI references. Framework
vocabulary (``lup_tool``, ``lup-devtools``, ``.lup/``, etc.) stays
unchanged.

Examples::

    $ uv run lup-devtools dev init rename-package myproject
    $ uv run lup-devtools dev init rename-package myproject --dry-run
"""

import re
from pathlib import Path
import sh
import typer

git = sh.Command("git")

PACKAGE_IMPORT_RE = re.compile(
    r"""
    (?<![.\w])          # not preceded by dot or word char
    (?:from|import)     # keyword
    \s+
    lup_template        # the package name
    (?=\.|\.|\s|$)      # followed by dot, whitespace, or end
    """,
    re.VERBOSE,
)

FRAMEWORK_MARKERS = {
    "lup_tool",
    "LupMcpTool",
    "lup-tools",
    "lup-devtools",
    "lup-sandbox",
    "lup-mcp",
    "lup@local",
    "lup-template",
    "plugins/lup",
    "plugins/cache/local/lup",
}


def find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    typer.echo("Error: Could not find pyproject.toml", err=True)
    raise typer.Exit(1)


def is_framework_reference(line: str) -> bool:
    """Check if a line's ``lup_template`` usage is framework vocabulary, not a package import."""
    return any(marker in line for marker in FRAMEWORK_MARKERS)


def rename_imports_in_file(path: Path, new_name: str) -> list[str]:
    """Rename ``from lup_template.`` / ``import lup_template`` imports in a single file.

    Returns a list of change descriptions (empty if no changes).
    """
    text = path.read_text()
    changes: list[str] = []

    def replace_import(m: re.Match[str]) -> str:
        full_match = m.group(0)
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start : line_end if line_end != -1 else len(text)]

        if is_framework_reference(line):
            return full_match

        replaced = full_match.replace("lup_template", new_name, 1)  # claude: ignore
        changes.append(f"  {path}: {full_match!r} -> {replaced!r}")
        return replaced

    new_text = PACKAGE_IMPORT_RE.sub(replace_import, text)
    if new_text != text:
        path.write_text(new_text)
    return changes


def rename_in_pyproject(path: Path, new_name: str) -> list[str]:
    """Update pyproject.toml: package name, CLI entry point, devtools import path."""
    text = path.read_text()
    changes: list[str] = []
    new_text = text

    old_name_line = 'name = "lup-template"'
    new_name_line = f'name = "{new_name}"'
    if old_name_line in new_text:
        new_text = new_text.replace(old_name_line, new_name_line, 1)
        changes.append(f"  package name: lup-template -> {new_name}")

    old_cli = 'lup = "lup_template.environment.cli.__main__:app"'
    new_cli = f'{new_name} = "{new_name}.environment.cli.__main__:app"'
    if old_cli in new_text:
        new_text = new_text.replace(old_cli, new_cli, 1)
        changes.append(f"  CLI entry point: lup -> {new_name}")

    old_devtools = 'lup-devtools = "lup_template.devtools.main:app"'
    new_devtools = f'lup-devtools = "{new_name}.devtools.main:app"'
    if old_devtools in new_text:
        new_text = new_text.replace(old_devtools, new_devtools, 1)
        changes.append(
            f"  devtools import path: lup_template.devtools -> {new_name}.devtools"
        )

    if new_text != text:
        path.write_text(new_text)
    return changes


def rename_cli_app_name(cli_path: Path, new_name: str) -> list[str]:
    """Update the Typer app name in the CLI module."""
    if not cli_path.exists():
        return []

    text = cli_path.read_text()
    old = 'name="lup"'
    if old not in text:
        return []

    text = text.replace(old, f'name="{new_name}"', 1)
    cli_path.write_text(text)
    return [f"  CLI app name: lup -> {new_name}"]


def rename_package(
    new_name: str,
    dry_run: bool,
) -> None:
    """Rename the lup_template Python package to a project-specific name."""
    if not new_name.isidentifier():
        typer.echo(f"Error: {new_name!r} is not a valid Python identifier", err=True)
        raise typer.Exit(1)

    if new_name == "lup_template":
        typer.echo("Error: new name is the same as the current name", err=True)
        raise typer.Exit(1)

    root = find_project_root()
    src_dir = root / "src"
    old_pkg = src_dir / "lup_template"
    new_pkg = src_dir / new_name

    if not old_pkg.is_dir():
        typer.echo(f"Error: {old_pkg} does not exist", err=True)
        raise typer.Exit(1)

    if new_pkg.exists():
        typer.echo(f"Error: {new_pkg} already exists", err=True)
        raise typer.Exit(1)

    all_changes: list[str] = []

    python_files: list[Path] = []
    for search_dir in [src_dir, root / "tests"]:
        if search_dir.is_dir():
            python_files.extend(search_dir.rglob("*.py"))

    typer.echo("Import renames:" if dry_run else "Renaming imports...")
    for py_file in sorted(python_files):
        changes = rename_imports_in_file(py_file, new_name) if not dry_run else []
        if dry_run:
            text = py_file.read_text()
            for m in PACKAGE_IMPORT_RE.finditer(text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line = text[line_start : line_end if line_end != -1 else len(text)]
                if not is_framework_reference(line):
                    changes.append(
                        f"  {py_file}: {m.group(0)!r} -> {m.group(0).replace('lup_template', new_name, 1)!r}"  # claude: ignore
                    )
        all_changes.extend(changes)

    pyproject = root / "pyproject.toml"
    typer.echo("\npyproject.toml:" if dry_run else "Updating pyproject.toml...")
    if dry_run:
        text = pyproject.read_text()
        if 'name = "lup-template"' in text:
            all_changes.append(f"  package name: lup-template -> {new_name}")
        if 'lup = "lup_template.environment.cli.__main__:app"' in text:
            all_changes.append(f"  CLI entry point: lup -> {new_name}")
        if 'lup-devtools = "lup_template.devtools.main:app"' in text:
            all_changes.append(
                f"  devtools import path: lup_template.devtools -> {new_name}.devtools"
            )
    else:
        all_changes.extend(rename_in_pyproject(pyproject, new_name))

    cli_path = old_pkg / "environment" / "cli" / "__main__.py"
    typer.echo("\nCLI app name:" if dry_run else "Updating CLI app name...")
    if dry_run:
        if cli_path.exists() and 'name="lup"' in cli_path.read_text():
            all_changes.append(f"  CLI app name: lup -> {new_name}")
    else:
        all_changes.extend(rename_cli_app_name(cli_path, new_name))

    if dry_run:
        typer.echo("\nDirectory rename:")
        all_changes.append(f"  src/lup_template/ -> src/{new_name}/")
    else:
        typer.echo("Renaming package directory...")
        git("mv", str(old_pkg), str(new_pkg), _cwd=str(root))
        all_changes.append(f"  src/lup_template/ -> src/{new_name}/")

    typer.echo()
    if dry_run:
        typer.echo(f"Dry run: {len(all_changes)} changes would be made:")
    else:
        typer.echo(f"Done: {len(all_changes)} changes made:")
    for change in all_changes:
        typer.echo(change)

    if not dry_run:
        typer.echo("\nNext steps:")
        typer.echo("  uv sync")
        typer.echo("  uv run pyright")
        typer.echo("  uv run ruff check .")
        typer.echo("  uv run pytest")
