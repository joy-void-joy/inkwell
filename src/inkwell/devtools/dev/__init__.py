"""Dev operations: worktrees, branches, and pre-flight checks."""

from typing import Annotated

import typer

import lup_template.devtools.dev.branches as branches
import lup_template.devtools.dev.check as check
import lup_template.devtools.dev.conflicts as conflicts
import lup_template.devtools.dev.init as init
import lup_template.devtools.dev.pr as pr
import lup_template.devtools.dev.worktree as worktree

app = typer.Typer(no_args_is_help=True)
worktree_app = typer.Typer(no_args_is_help=True)
pr_app = typer.Typer(no_args_is_help=True)
init_app = typer.Typer(no_args_is_help=True)
app.add_typer(worktree_app, name="worktree", help="Worktree management")
app.add_typer(pr_app, name="pr", help="PR lifecycle (status, merge, push, checks)")
app.add_typer(init_app, name="init", help="Project initialization")


# -- worktree commands --


@worktree_app.command("create")
def worktree_create_cmd(
    name: Annotated[
        str, typer.Argument(help="Name for the worktree (e.g., feat-name)")
    ],
    no_sync: Annotated[
        bool,
        typer.Option("--no-sync", help="Skip running uv sync"),
    ] = False,
    no_copy_data: Annotated[
        bool,
        typer.Option("--no-copy-data", help="Skip copying gitignored extras"),
    ] = False,
    no_plugin_refresh: Annotated[
        bool,
        typer.Option(
            "--no-plugin-refresh", help="Skip plugin cache refresh and install"
        ),
    ] = False,
    base_branch: Annotated[
        str | None,
        typer.Option("--base", "-b", help="Base branch (default: current branch)"),
    ] = None,
) -> None:
    """Create or re-attach a git worktree."""
    worktree.create(name, no_sync, no_copy_data, no_plugin_refresh, base_branch)


@worktree_app.command("list")
def worktree_list_cmd() -> None:
    """List all git worktrees with branch and status info."""
    worktree.list_worktrees()


@worktree_app.command("remove")
def worktree_remove_cmd(
    name: Annotated[str, typer.Argument(help="Worktree name or path to remove")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Force removal even if dirty"),
    ] = False,
) -> None:
    """Remove a git worktree."""
    worktree.remove(name, force)


# -- branch commands --


@app.command("branches")
def branches_cmd(
    branch: Annotated[
        str | None,
        typer.Argument(help="Specific branch to check (default: all)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Analyze branch containment, PR status, and worktree info."""
    branches.branch_status(branch, as_json)


@app.command("base-branch")
def base_branch_cmd(
    branch: Annotated[
        str | None,
        typer.Argument(help="Branch to analyze (default: current)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Detect the base branch for the current (or specified) branch."""
    branches.base_branch(branch, as_json)


@app.command("pr-status")
def pr_status_cmd(
    branch: Annotated[
        str | None,
        typer.Argument(help="Branch to check PR for (default: current)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show PR review status, checks, and merge readiness."""
    branches.pr_status(branch, as_json)


# -- pr-body command --


@app.command("pr-body")
def pr_body_cmd(
    base: Annotated[
        str | None,
        typer.Option("--base", "-b", help="Override base branch"),
    ] = None,
) -> None:
    """Generate a PR body (summary, commits, test plan) from branch commits."""
    branches.pr_body(base)


# -- branch survey and delete --


@app.command("survey")
def survey_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Full branch inventory: containment, PRs, unique commits, diff sizes."""
    branches.survey(as_json)


@app.command("delete")
def delete_cmd(
    name: Annotated[str, typer.Argument(help="Branch name to delete")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would happen"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force delete (git branch -D)"),
    ] = False,
) -> None:
    """Delete a branch, its worktree, and remote tracking branch."""
    branches.delete_branch(name, dry_run, force)


# -- conflict commands --


@app.command("conflicts")
def conflicts_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show conflicted files with scope classification (in-scope vs out-of-scope)."""
    conflicts.conflicts(as_json)


@app.command("conflict-status")
def conflict_status_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Detect conflict state, list files, and show both sides' history."""
    conflicts.conflict_status(as_json)


@app.command("conflict-audit")
def conflict_audit_cmd(
    files: Annotated[
        list[str],
        typer.Argument(help="Files to audit for accidental deletions"),
    ],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Post-resolution deletion audit: check for accidentally dropped code."""
    conflicts.conflict_audit(files, as_json)


@app.command("conflict-complete")
def conflict_complete_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would happen"),
    ] = False,
) -> None:
    """Finalize the merge/rebase/cherry-pick after all conflicts are resolved."""
    conflicts.conflict_complete(dry_run)


# -- check command --


@app.command("check")
def check_cmd(
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Auto-fix ruff issues"),
    ] = False,
    no_test: Annotated[
        bool,
        typer.Option("--no-test", help="Skip pytest"),
    ] = False,
) -> None:
    """Run pyright, ruff, and pytest."""
    check.run_checks(fix, no_test)


# -- init commands --


@init_app.command("rename-package")
def init_rename_package_cmd(
    new_name: Annotated[
        str,
        typer.Argument(
            help="New package name (valid Python identifier, e.g. 'aib', 'forecast_bot')"
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would change without modifying files"
        ),
    ] = False,
) -> None:
    """Rename the lup Python package to a project-specific name."""
    init.rename_package(new_name, dry_run)


# -- pr commands --


@pr_app.command("status")
def pr_status_detail_cmd(
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch name (default: current branch)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Fetch PR review status, checks, and comments for a branch."""
    pr.status(branch, as_json)


@pr_app.command("merge")
def pr_merge_cmd(
    pr_number: Annotated[int, typer.Argument(help="PR number to merge")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would happen"),
    ] = False,
) -> None:
    """Squash-merge a PR and pull changes into the integration branch."""
    pr.merge(pr_number, dry_run)


@pr_app.command("sync-base")
def pr_sync_base_cmd(
    base: Annotated[
        str | None,
        typer.Option("--base", "-b", help="Base branch (default: auto-detect)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Sync the base branch and merge it into the current feature branch."""
    pr.sync_base(base, as_json)


@pr_app.command("push")
def pr_push_cmd(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force push"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Push the current branch and report any existing PR."""
    pr.push(force, as_json)


@pr_app.command("create")
def pr_create_cmd(
    base: Annotated[str, typer.Option("--base", help="Target branch for PR")],
    title: Annotated[str, typer.Option("--title", help="PR title")],
    body: Annotated[str, typer.Option("--body", help="PR body (markdown)")],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Create a new PR."""
    pr.create(base, title, body, as_json)


@pr_app.command("update")
def pr_update_cmd(
    pr_number: Annotated[int, typer.Argument(help="PR number to update")],
    body: Annotated[str, typer.Option("--body", help="New PR body (markdown)")],
) -> None:
    """Update a PR body."""
    pr.update(pr_number, body)


@pr_app.command("checks")
def pr_checks_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Run pyright, ruff, and pytest validation checks."""
    pr.checks(as_json)
