"""Conflict scope classification, audit, and completion for merge/rebase conflicts.

After a failed merge or rebase, classifies conflicted files as in-scope
(touched by this branch) or out-of-scope (only changed on the other side).

Examples::

    $ uv run lup-devtools dev conflicts
    $ uv run lup-devtools dev conflicts --json
    $ uv run lup-devtools dev conflict-status --json
    $ uv run lup-devtools dev conflict-audit src/lup/agent/core.py --json
    $ uv run lup-devtools dev conflict-complete --dry-run
"""

import json
import re
from pathlib import Path
from typing import TypedDict

import sh
import typer
from pydantic import BaseModel


class ConflictFile(TypedDict):
    path: str
    conflict_count: int
    scope: str
    branch_touched: bool


class ConflictReport(TypedDict):
    state: str
    base: str
    files: list[ConflictFile]
    in_scope_count: int
    out_of_scope_count: int


def find_git_dir() -> Path:
    """Locate the .git directory (works in worktrees too)."""
    git = sh.Command("git")
    return Path(str(git("rev-parse", "--git-dir")).strip())


def detect_conflict_state() -> str | None:
    """Detect whether we're in a merge, rebase, or cherry-pick."""
    git_dir = find_git_dir()
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return None


def get_branch_files(state: str) -> tuple[str, set[str]]:
    """Return (merge_base, files touched by this branch) for scope classification."""
    git = sh.Command("git")
    git_dir = find_git_dir()

    match state:
        case "merge":
            merge_head = str(git("rev-parse", "MERGE_HEAD")).strip()
            base = str(git("merge-base", "HEAD", merge_head)).strip()
            files_output = str(
                git("diff", "--name-only", f"{base}..HEAD", _ok_code=[0])
            ).strip()

        case "rebase":
            rebase_merge = git_dir / "rebase-merge"
            rebase_apply = git_dir / "rebase-apply"
            if rebase_merge.exists():
                onto = (rebase_merge / "onto").read_text().strip()
                orig_head = (rebase_merge / "head").read_text().strip()
            elif rebase_apply.exists():
                onto = (rebase_apply / "onto").read_text().strip()
                orig_head = (rebase_apply / "orig-head").read_text().strip()
            else:
                typer.echo("Cannot determine rebase state", err=True)
                raise typer.Exit(1)
            base = str(git("merge-base", orig_head, onto)).strip()
            files_output = str(
                git("diff", "--name-only", f"{base}..{orig_head}", _ok_code=[0])
            ).strip()

        case "cherry-pick":
            cherry_head = str(git("rev-parse", "CHERRY_PICK_HEAD")).strip()
            base = str(git("merge-base", "HEAD", cherry_head)).strip()
            files_output = str(
                git("diff", "--name-only", f"{base}..HEAD", _ok_code=[0])
            ).strip()

        case _:
            typer.echo(f"Unknown conflict state: {state}", err=True)
            raise typer.Exit(1)

    files = set(files_output.splitlines()) if files_output else set()
    return base, files


def list_conflicted_files() -> list[str]:
    """List files with unresolved conflicts."""
    git = sh.Command("git")
    output = str(git("diff", "--name-only", "--diff-filter=U", _ok_code=[0])).strip()
    if not output:
        return []
    return output.splitlines()


def count_conflict_markers(path: str) -> int:
    """Count the number of conflict marker blocks in a file."""
    try:
        content = Path(path).read_text(encoding="utf-8")
        return content.count("<<<<<<<")
    except OSError:
        return 0


def build_conflict_report(state: str) -> ConflictReport:
    """Build a structured conflict scope report."""
    conflicted = list_conflicted_files()
    base, branch_files = get_branch_files(state)

    files: list[ConflictFile] = []
    in_scope = 0
    out_of_scope = 0

    for path in conflicted:
        touched = path in branch_files
        scope = "in-scope" if touched else "out-of-scope"
        markers = count_conflict_markers(path)

        if touched:
            in_scope += 1
        else:
            out_of_scope += 1

        files.append(
            {
                "path": path,
                "conflict_count": markers,
                "scope": scope,
                "branch_touched": touched,
            }
        )

    return {
        "state": state,
        "base": base,
        "files": files,
        "in_scope_count": in_scope,
        "out_of_scope_count": out_of_scope,
    }


def conflicts(as_json: bool) -> None:
    """Show conflicted files with scope classification."""
    state = detect_conflict_state()
    if not state:
        typer.echo("Not in a merge, rebase, or cherry-pick state", err=True)
        raise typer.Exit(1)

    conflicted = list_conflicted_files()
    if not conflicted:
        typer.echo("No conflicted files found")
        return

    report = build_conflict_report(state)

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return

    typer.echo(f"\nConflict state: {report['state']}")
    typer.echo(f"Merge base: {report['base'][:10]}")
    typer.echo(f"\n{'File':<50} {'Conflicts':>10} {'Scope':>15}")
    typer.echo("-" * 78)

    for f in report["files"]:
        typer.echo(f"{f['path']:<50} {f['conflict_count']:>10} {f['scope']:>15}")

    typer.echo(
        f"\nIn-scope: {report['in_scope_count']}, "
        f"Out-of-scope: {report['out_of_scope_count']}"
    )


# -- Status, audit, and completion --


git = sh.Command("git").bake("--no-pager")


class ConflictStatusResult(BaseModel):
    operation: str
    conflicted_files: list[str]
    ours_ref: str
    theirs_ref: str
    ours_commits: list[str]
    theirs_commits: list[str]


class FileAuditResult(BaseModel):
    path: str
    ours_removals: list[str]
    theirs_removals: list[str]
    warning: bool


class AuditResult(BaseModel):
    files: list[FileAuditResult]
    has_warnings: bool


SIGNIFICANT_PATTERN = re.compile(
    r"^-(def |class |async def |@app\.|@[a-z]+_tool|    def )"
)


def detect_operation() -> str:
    """Detect if we're in a merge, rebase, or cherry-pick."""
    git_dir = find_git_dir()

    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return "none"


def extract_removals(diff_output: str) -> list[str]:
    """Find removed functions/classes/decorators in a diff."""
    removals: list[str] = []
    for line in diff_output.splitlines():
        if SIGNIFICANT_PATTERN.match(line):
            removals.append(line.lstrip("-").strip())
    return removals


def conflict_status(as_json: bool) -> None:
    """Detect conflict state, list files, and show both sides' history."""
    operation = detect_operation()

    if operation == "none":
        if as_json:
            result = ConflictStatusResult(
                operation="none",
                conflicted_files=[],
                ours_ref="HEAD",
                theirs_ref="",
                ours_commits=[],
                theirs_commits=[],
            )
            print(result.model_dump_json(indent=2))
        else:
            typer.echo("No merge/rebase/cherry-pick in progress")
        return

    conflicted = list_conflicted_files()

    ours_ref = "HEAD"
    theirs_ref = "MERGE_HEAD" if operation == "merge" else "CHERRY_PICK_HEAD"

    ours_commits: list[str] = []
    theirs_commits: list[str] = []

    if operation == "merge":
        try:
            merge_base = str(
                git("merge-base", "HEAD", "MERGE_HEAD", _ok_code=[0])
            ).strip()
            ours_raw = str(
                git("log", "--oneline", f"{merge_base}..HEAD", _ok_code=[0])
            ).strip()
            ours_commits = [line for line in ours_raw.splitlines() if line][:10]

            theirs_raw = str(
                git("log", "--oneline", f"{merge_base}..MERGE_HEAD", _ok_code=[0])
            ).strip()
            theirs_commits = [line for line in theirs_raw.splitlines() if line][:10]
        except sh.ErrorReturnCode:
            pass

    result = ConflictStatusResult(
        operation=operation,
        conflicted_files=conflicted,
        ours_ref=ours_ref,
        theirs_ref=theirs_ref,
        ours_commits=ours_commits,
        theirs_commits=theirs_commits,
    )

    if as_json:
        print(result.model_dump_json(indent=2))
    else:
        typer.echo(f"Operation: {operation}")
        typer.echo(f"Conflicted files ({len(conflicted)}):")
        for f in conflicted:
            typer.echo(f"  {f}")
        if ours_commits:
            typer.echo(f"\nOurs ({ours_ref}):")
            for c in ours_commits:
                typer.echo(f"  {c}")
        if theirs_commits:
            typer.echo(f"\nTheirs ({theirs_ref}):")
            for c in theirs_commits:
                typer.echo(f"  {c}")


def conflict_audit(files: list[str], as_json: bool) -> None:
    """Post-resolution deletion audit: check for accidentally dropped code."""
    operation = detect_operation()
    if operation == "none":
        typer.echo("No merge/rebase/cherry-pick in progress", err=True)
        raise typer.Exit(1)

    theirs_ref = "MERGE_HEAD" if operation == "merge" else "CHERRY_PICK_HEAD"

    file_results: list[FileAuditResult] = []
    for path in files:
        ours_diff = str(git("diff", "HEAD", "--", path, _ok_code=[0, 1])).strip()
        ours_removals = extract_removals(ours_diff)

        theirs_diff = ""
        try:
            theirs_diff = str(
                git("diff", theirs_ref, "--", path, _ok_code=[0, 1])
            ).strip()
        except sh.ErrorReturnCode:
            pass
        theirs_removals = extract_removals(theirs_diff)

        has_warning = bool(ours_removals or theirs_removals)
        file_results.append(
            FileAuditResult(
                path=path,
                ours_removals=ours_removals,
                theirs_removals=theirs_removals,
                warning=has_warning,
            )
        )

    audit_result = AuditResult(
        files=file_results,
        has_warnings=any(f.warning for f in file_results),
    )

    if as_json:
        print(audit_result.model_dump_json(indent=2))
    else:
        for f in file_results:
            status = "WARNING" if f.warning else "OK"
            typer.echo(f"  {f.path}: {status}")
            for r in f.ours_removals:
                typer.echo(f"    - [ours] {r}")
            for r in f.theirs_removals:
                typer.echo(f"    - [theirs] {r}")

        if audit_result.has_warnings:
            typer.echo("\nSome files have removals — review before completing.")


def conflict_complete(dry_run: bool) -> None:
    """Finalize the merge/rebase/cherry-pick after all conflicts are resolved."""
    operation = detect_operation()
    if operation == "none":
        typer.echo("No merge/rebase/cherry-pick in progress")
        return

    remaining = list_conflicted_files()
    if remaining:
        typer.echo(f"Error: {len(remaining)} conflicted file(s) remain:", err=True)
        for f in remaining:
            typer.echo(f"  {f}", err=True)
        raise typer.Exit(1)

    match operation:
        case "merge":
            cmd_desc = "git commit --no-edit"
        case "rebase":
            cmd_desc = "git rebase --continue"
        case "cherry-pick":
            cmd_desc = "git cherry-pick --continue"
        case _:
            typer.echo(f"Error: unknown operation {operation!r}", err=True)
            raise typer.Exit(1)

    if dry_run:
        typer.echo(f"Would run: {cmd_desc}")
        return

    try:
        match operation:
            case "merge":
                git("commit", "--no-edit")
            case "rebase":
                git("rebase", "--continue")
            case "cherry-pick":
                git("cherry-pick", "--continue")
        typer.echo(f"Completed {operation}")
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Failed to complete {operation}: {stderr}", err=True)
        raise typer.Exit(1)
