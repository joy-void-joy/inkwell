"""Unified pre-flight checks: ruff, pyright, pytest."""

import sys

import typer


def run_checks(fix: bool, no_test: bool) -> None:
    """Run ruff format, ruff check, pyright, and pytest in sequence."""
    import sh

    uv = sh.Command("uv")
    results: list[tuple[str, bool]] = []

    # ruff format
    try:
        if fix:
            uv("run", "ruff", "format", ".")
            typer.echo("ruff format: applied")
        else:
            uv("run", "ruff", "format", "--check", ".")
            typer.echo("ruff format: ok")
        results.append(("ruff format", True))
    except sh.ErrorReturnCode:
        typer.echo("ruff format: FAIL")
        results.append(("ruff format", False))

    # ruff check
    try:
        args = ["run", "ruff", "check", "."]
        if fix:
            args.append("--fix")
        uv(*args)
        typer.echo("ruff check: ok")
        results.append(("ruff check", True))
    except sh.ErrorReturnCode:
        typer.echo("ruff check: FAIL")
        results.append(("ruff check", False))

    # pyright
    try:
        uv("run", "pyright")
        typer.echo("pyright: ok")
        results.append(("pyright", True))
    except sh.ErrorReturnCode:
        typer.echo("pyright: FAIL")
        results.append(("pyright", False))

    # pytest
    if not no_test:
        try:
            uv("run", "pytest")
            typer.echo("pytest: ok")
            results.append(("pytest", True))
        except sh.ErrorReturnCode:
            typer.echo("pytest: FAIL")
            results.append(("pytest", False))

    # summary
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    typer.echo(f"\n{passed}/{total} checks passed")

    if any(not ok for _, ok in results):
        failed = [name for name, ok in results if not ok]
        typer.echo(f"Failed: {', '.join(failed)}")
        sys.exit(1)
