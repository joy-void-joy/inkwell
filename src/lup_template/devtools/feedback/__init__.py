"""Feedback state: collection, analysis marks, metrics, and commit operations."""

from pathlib import Path
from typing import Annotated

import typer

import lup_template.devtools.feedback.analyze as analyze
import lup_template.devtools.feedback.state as state
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


@app.command("status")
def status_cmd(
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """Show feedback status: version, data, analysis state, and aggregate stats."""
    state.status(version, all_versions)


@app.command("collect")
def collect_cmd(
    since: Annotated[
        str | None,
        typer.Option(
            "--since", "-s", help="Only sessions after this date (YYYY-MM-DD)"
        ),
    ] = None,
    all_time: Annotated[
        bool,
        typer.Option("--all-time", help="Include all sessions regardless of date"),
    ] = False,
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file path"),
    ] = None,
) -> None:
    """Collect feedback metrics from sessions."""
    state.collect(since, all_time, version, all_versions, output)


@app.command("tools")
def tools_cmd(
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """Show tool usage aggregates."""
    state.tools(version, all_versions)


@app.command("errors")
def errors_cmd(
    limit: int = typer.Option(20, "-n", "--limit", help="Max errors to show"),
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """Show sessions with high error rates from structured metrics."""
    state.errors(limit, version, all_versions)


@app.command("trends")
def trends_cmd(
    window: Annotated[
        int,
        typer.Option("-w", "--window", help="Rolling window size"),
    ] = 10,
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """Show metric trends over time."""
    state.trends(window, version, all_versions)


@app.command("history")
def history_cmd(
    limit: Annotated[
        int,
        typer.Option("-n", "--limit", help="Max to show"),
    ] = 10,
) -> None:
    """Show previous feedback collection runs."""
    state.history(limit)


@app.command("mark")
def mark_cmd(
    session_ids: Annotated[
        list[str], typer.Argument(help="Session IDs to mark as analyzed")
    ],
) -> None:
    """Mark sessions as analyzed in the feedback loop."""
    state.mark(session_ids)


@app.command("unmark")
def unmark_cmd(
    session_ids: Annotated[list[str], typer.Argument(help="Session IDs to unmark")],
) -> None:
    """Remove analysis marks from sessions."""
    state.unmark(session_ids)


@app.command("prompt-health")
def prompt_health_cmd() -> None:
    """Analyze the agent prompt for size and patch accumulation."""
    state.prompt_health()


@app.command("unanalyzed")
def unanalyzed_cmd(
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
) -> None:
    """List unanalyzed session IDs, one per line."""
    state.unanalyzed(version, all_versions)


@app.command("analyze")
def analyze_cmd(
    version: VERSION_OPT = AGENT_VERSION,
    all_versions: ALL_VERSIONS_OPT = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Write JSON report to file instead of stdout"
        ),
    ] = None,
) -> None:
    """Produce a structured JSON analysis report (tool health, errors, capability gaps)."""
    analyze.analyze(version, all_versions, output)


@app.command("commit")
def commit_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show what would be committed"
    ),
) -> None:
    """Commit all uncommitted session result files, one commit per session."""
    state.commit(dry_run)
