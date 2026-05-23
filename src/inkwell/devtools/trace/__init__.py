"""Trace display, search, and analysis."""

from typing import Annotated

import typer

import lup_template.devtools.trace.traces as traces
from lup.history import resolve_version
from lup.paths import AGENT_VERSION

app = typer.Typer(no_args_is_help=True)

VERSION_OPT = Annotated[
    str | None,
    typer.Option("--version", "-v", help="Agent version (default: current)"),
]
ALL_VERSIONS_OPT = Annotated[
    bool,
    typer.Option("--all-versions", help="Include all versions"),
]


@app.command("show")
def show_cmd(
    session_id: str = typer.Argument(..., help="Session ID to show trace for"),
    full: bool = typer.Option(False, "-f", "--full", help="Show full trace"),
    tool_calls: bool = typer.Option(
        False, "--tool-calls", "-t", help="Show only tool call blocks"
    ),
) -> None:
    """Show trace for a session."""
    traces.show(session_id, full, tool_calls)


@app.command("search")
def search_cmd(
    pattern: str = typer.Argument(..., help="Pattern to search for (regex)"),
    context: int = typer.Option(2, "-C", help="Lines of context around match"),
) -> None:
    """Search traces for a pattern."""
    traces.search(pattern, context)


@app.command("list")
def list_cmd(
    limit: int = typer.Option(20, "-n", "--limit", help="Max to show"),
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """List available traces."""
    effective, warning = resolve_version(version, all_versions)
    if warning:
        typer.echo(warning)
    traces.list_traces(limit, effective)


@app.command("errors")
def errors_cmd(
    limit: int = typer.Option(20, "-n", "--limit", help="Max errors to show"),
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """Show sessions with errors found in trace files."""
    effective, warning = resolve_version(version, all_versions)
    if warning:
        typer.echo(warning)
    traces.errors_in_traces(limit, effective)


@app.command("capabilities")
def capabilities_cmd() -> None:
    """Extract capability requests from traces."""
    traces.capabilities()
