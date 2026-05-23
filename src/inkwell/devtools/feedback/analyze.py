"""Feedback analysis: structured report combining tool health, errors, and capability gaps.

Examples::

    $ uv run lup-devtools feedback analyze
    $ uv run lup-devtools feedback analyze --version 0.3.0
    $ uv run lup-devtools feedback analyze --output report.json
"""

# claude: ignore

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, TypedDict

from lup_template.devtools.feedback.state import load_sessions_for_versions
from lup.history import resolve_version
from lup.paths import traces_path


class ToolHealth(TypedDict):
    name: str
    calls: int
    errors: int
    error_rate: float


class ErrorPattern(TypedDict):
    session_id: str
    error_count: int
    total_calls: int
    error_rate: float
    top_errors: list[str]


class CapabilityGap(TypedDict):
    request: str
    count: int
    session_ids: list[str]


class AnalysisReport(TypedDict):
    version: str | None
    tool_health: list[ToolHealth]
    error_patterns: list[ErrorPattern]
    capability_gaps: list[CapabilityGap]


def gather_tool_health(sessions: list[dict[str, Any]]) -> list[ToolHealth]:
    """Compute per-tool call counts, error counts, and error rates."""
    tool_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "errors": 0}
    )

    for s in sessions:
        metrics = s.get("tool_metrics", {})
        by_tool = metrics.get("by_tool", {})
        for tool_name, data in by_tool.items():
            tool_stats[tool_name]["calls"] += data.get("call_count", 0)
            tool_stats[tool_name]["errors"] += data.get("error_count", 0)

    result: list[ToolHealth] = []
    for name in sorted(tool_stats, key=lambda t: -tool_stats[t]["calls"]):
        stats = tool_stats[name]
        calls = stats["calls"]
        errors = stats["errors"]
        result.append(
            {
                "name": name,
                "calls": calls,
                "errors": errors,
                "error_rate": (errors / calls) if calls > 0 else 0.0,
            }
        )
    return result


def gather_error_patterns(sessions: list[dict[str, Any]]) -> list[ErrorPattern]:
    """Find sessions with high error rates, grouped by error type."""
    result: list[ErrorPattern] = []

    for s in sessions:
        metrics = s.get("tool_metrics", {})
        total_errors = metrics.get("total_errors", 0)
        if not total_errors or total_errors <= 0:
            continue

        total_calls = metrics.get("total_tool_calls", 0)
        by_tool = metrics.get("by_tool", {})

        tool_errors: list[tuple[int, str]] = []
        for tool_name, tool_data in by_tool.items():
            errs = tool_data.get("error_count", 0)
            if errs > 0:
                tool_errors.append((errs, tool_name))
        tool_errors.sort(reverse=True)

        result.append(
            {
                "session_id": s.get("_session_id", ""),
                "error_count": total_errors,
                "total_calls": total_calls,
                "error_rate": (total_errors / total_calls) if total_calls > 0 else 0.0,
                "top_errors": [f"{name}: {count}" for count, name in tool_errors],
            }
        )

    result.sort(key=lambda x: -x["error_count"])
    return result


CAPABILITY_PATTERNS = re.compile(
    r"would be useful|would have helped|would benefit from|wish I had|"
    r"if I could|tool that|need.* access to|cannot .* because",
    re.IGNORECASE,
)


def gather_capability_gaps() -> list[CapabilityGap]:
    """Extract and aggregate capability requests from trace files."""
    if not traces_path().exists():
        return []

    requests_by_text: dict[str, list[str]] = defaultdict(list)

    for trace_file in traces_path().rglob("*.md"):
        try:
            content = trace_file.read_text(encoding="utf-8")
            rel = trace_file.relative_to(traces_path())
            session_id = rel.parts[2] if len(rel.parts) > 2 else rel.stem

            for line in content.split("\n"):
                if CAPABILITY_PATTERNS.search(line):
                    text = line.strip()[:120]
                    if text:
                        requests_by_text[text].append(session_id)
        except (OSError, ValueError):
            pass

    result: list[CapabilityGap] = []
    for request, session_ids in sorted(
        requests_by_text.items(), key=lambda x: -len(x[1])
    ):
        result.append(
            {
                "request": request,
                "count": len(session_ids),
                "session_ids": sorted(set(session_ids)),
            }
        )
    return result


def build_report(version: str | None, all_versions: bool) -> AnalysisReport:
    """Build a complete analysis report."""
    effective, _ = resolve_version(version, all_versions)
    sessions = load_sessions_for_versions(effective)

    return {
        "version": version,
        "tool_health": gather_tool_health(sessions),
        "error_patterns": gather_error_patterns(sessions),
        "capability_gaps": gather_capability_gaps(),
    }


def analyze(version: str | None, all_versions: bool, output: Path | None) -> None:
    """Produce a JSON analysis report to stdout or a file."""
    import json

    import typer

    report = build_report(version, all_versions)
    report_json = json.dumps(report, indent=2)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report_json + "\n")
        typer.echo(f"Report written to {output}")
    else:
        typer.echo(report_json)
