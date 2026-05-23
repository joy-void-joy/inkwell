"""Agent version display, changelog, and bump.

Examples::

    $ uv run lup-devtools version
    $ uv run lup-devtools version --json
    $ uv run lup-devtools version changelog
    $ uv run lup-devtools version changelog --json
    $ uv run lup-devtools version bump minor
"""

import json
from typing import Annotated, TypedDict

import sh
import typer

from lup.paths import AGENT_VERSION

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)


class VersionInfo(TypedDict):
    version: str
    latest_tag: str | None
    commits_since_tag: int
    files_changed: list[str]


class ChangelogEntry(TypedDict):
    sha: str
    message: str
    category: str


class ChangelogReport(TypedDict):
    since_tag: str | None
    behavior: list[ChangelogEntry]
    data: list[ChangelogEntry]
    infrastructure: list[ChangelogEntry]


BEHAVIOR_PREFIXES = ("feat", "fix", "refactor")
DATA_PREFIXES = ("data",)


def get_latest_tag() -> str | None:
    git = sh.Command("git")
    try:
        return str(git("describe", "--tags", "--abbrev=0", _ok_code=[0])).strip()
    except sh.ErrorReturnCode:
        return None


def classify_commit(message: str) -> str:
    lower = message.lower()
    for prefix in BEHAVIOR_PREFIXES:
        if lower.startswith(prefix):
            return "behavior"
    for prefix in DATA_PREFIXES:
        if lower.startswith(prefix):
            return "data"
    return "infrastructure"


@app.callback()
def show(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show agent version, latest tag, and commits since last tag."""
    import click

    if click.get_current_context().invoked_subcommand is not None:
        return

    git = sh.Command("git")
    latest_tag = get_latest_tag()

    commits_since = 0
    files_changed: list[str] = []
    ref_since = latest_tag or "HEAD~50"
    if latest_tag:
        try:
            commits_since = int(
                str(git("rev-list", "--count", f"{latest_tag}..HEAD")).strip()
            )
        except sh.ErrorReturnCode:
            pass

    try:
        diff_output = str(
            git("diff", "--name-only", f"{ref_since}..HEAD", _ok_code=[0, 128])
        ).strip()
        files_changed = [f for f in diff_output.splitlines() if f]
    except sh.ErrorReturnCode:
        pass

    if as_json:
        info: VersionInfo = {
            "version": AGENT_VERSION,
            "latest_tag": latest_tag,
            "commits_since_tag": commits_since,
            "files_changed": files_changed,
        }
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"\nAgent version: {AGENT_VERSION}")
    if latest_tag:
        typer.echo(f"Latest tag: {latest_tag} (+{commits_since} commits)")
    else:
        typer.echo("Latest tag: (none)")
    if files_changed:
        typer.echo(f"Files changed: {len(files_changed)}")


@app.command("changelog")
def changelog_cmd(
    since: Annotated[
        str | None,
        typer.Option(
            "--since", "-s", help="Tag or commit to start from (default: latest tag)"
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show changes since a version tag, classified by type."""
    git = sh.Command("git")

    tag = since or get_latest_tag()
    if not tag:
        ref = (
            str(git("rev-list", "--max-parents=0", "HEAD", _ok_code=[0]))
            .strip()
            .split("\n")[0]
        )
        tag = ref

    try:
        log_output = str(git("log", "--oneline", f"{tag}..HEAD", _ok_code=[0])).strip()
    except sh.ErrorReturnCode:
        typer.echo(f"Could not read log since {tag}")
        raise typer.Exit(1)

    if not log_output:
        typer.echo(f"No commits since {tag}")
        return

    report: ChangelogReport = {
        "since_tag": since or get_latest_tag(),
        "behavior": [],
        "data": [],
        "infrastructure": [],
    }

    for line in log_output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        message = parts[1] if len(parts) > 1 else ""
        category = classify_commit(message)
        entry: ChangelogEntry = {"sha": sha, "message": message, "category": category}
        report[category].append(entry)  # type: ignore[literal-required]

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return

    tag_display = since or get_latest_tag() or "(root)"
    typer.echo(f"\n=== Changes since {tag_display} ===\n")

    if report["behavior"]:
        typer.echo("Behavior changes:")
        for e in report["behavior"]:
            typer.echo(f"  {e['sha'][:7]} {e['message']}")

    if report["data"]:
        typer.echo("\nData changes:")
        for e in report["data"]:
            typer.echo(f"  {e['sha'][:7]} {e['message']}")

    if report["infrastructure"]:
        typer.echo("\nInfrastructure changes:")
        for e in report["infrastructure"]:
            typer.echo(f"  {e['sha'][:7]} {e['message']}")

    total = (
        len(report["behavior"]) + len(report["data"]) + len(report["infrastructure"])
    )
    typer.echo(f"\nTotal: {total} commits ({len(report['behavior'])} behavior)")


@app.command("bump")
def bump_cmd(
    level: Annotated[
        str | None,
        typer.Argument(help="Bump level: patch, minor, or major"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output result as JSON"),
    ] = False,
) -> None:
    """Bump agent version and create a git tag."""
    import re
    from pathlib import Path

    git = sh.Command("git")

    version_file = Path("src") / "lup" / "version.py"
    if not version_file.exists():
        for candidate in Path("src").glob("*/version.py"):
            version_file = candidate
            break

    if not version_file.exists():
        typer.echo("Could not find version.py")
        raise typer.Exit(1)

    content = version_file.read_text()
    match = re.search(r'AGENT_VERSION\s*=\s*"([^"]+)"', content)
    if not match:
        typer.echo("Could not parse AGENT_VERSION from version.py")
        raise typer.Exit(1)

    current = match.group(1)
    parts = current.split(".")
    if len(parts) != 3:
        typer.echo(f"Version {current} is not in X.Y.Z format")
        raise typer.Exit(1)

    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if level is None:
        typer.echo(f"Current version: {current}")
        typer.echo("Specify bump level: patch, minor, or major")
        raise typer.Exit(1)

    match level:
        case "patch":
            new_version = f"{major}.{minor}.{patch + 1}"
        case "minor":
            new_version = f"{major}.{minor + 1}.0"
        case "major":
            new_version = f"{major + 1}.0.0"
        case _:
            typer.echo(f"Unknown bump level: {level}. Use patch, minor, or major.")
            raise typer.Exit(1)

    new_content = content.replace(
        f'AGENT_VERSION = "{current}"', f'AGENT_VERSION = "{new_version}"'
    )
    version_file.write_text(new_content)

    git.add(str(version_file))
    git.commit("-m", f"chore(version): bump {current} → {new_version}")
    git.tag(f"v{new_version}")

    if as_json:
        typer.echo(
            json.dumps({"old": current, "new": new_version, "tag": f"v{new_version}"})
        )
    else:
        typer.echo(f"\nBumped: {current} → {new_version}")
        typer.echo(f"Tag: v{new_version}")
