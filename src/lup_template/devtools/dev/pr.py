"""PR lifecycle: status, merge, push, checks, and base sync.

Mechanical helpers for ``/lup:close`` and ``/lup:rebase``.

Examples::

    $ uv run lup-devtools dev pr status --json
    $ uv run lup-devtools dev pr merge 42
    $ uv run lup-devtools dev pr sync-base --json
    $ uv run lup-devtools dev pr push --force --json
    $ uv run lup-devtools dev pr create --base dev --title "feat: search" --body "..."
    $ uv run lup-devtools dev pr update 42 --body "..."
    $ uv run lup-devtools dev pr checks --json
"""

import json
import logging
import sys
from pathlib import Path
import sh
import typer
from pydantic import BaseModel

from lup_template.devtools.dev.branches import (
    detect_base_branch,
    get_integration_branch,
)

logger = logging.getLogger(__name__)

git = sh.Command("git").bake("--no-pager")
gh = sh.Command("gh").bake(_tty_out=False)
uv = sh.Command("uv")


def current_branch() -> str:
    return str(git("branch", "--show-current")).strip()


class ReviewInfo(BaseModel):
    author: str
    state: str
    body: str


class CheckInfo(BaseModel):
    name: str
    status: str
    conclusion: str


class PRInfo(BaseModel):
    number: int
    title: str
    url: str
    review_decision: str
    mergeable: str
    checks_passing: bool
    reviews: list[ReviewInfo]
    checks: list[CheckInfo]


class PRStatusResult(BaseModel):
    branch: str
    pr: PRInfo | None


class MergeResult(BaseModel):
    pr_number: int
    merged: bool
    integration_branch: str
    pulled: bool


class SyncBaseResult(BaseModel):
    feature_branch: str
    base_branch: str
    merged: bool
    conflicts: list[str]


class PushResult(BaseModel):
    branch: str
    pushed: bool
    force: bool
    existing_pr: dict[str, str | int] | None


class CreateResult(BaseModel):
    number: int
    url: str


class CheckResult(BaseModel):
    name: str
    passed: bool
    output: str


class ChecksResult(BaseModel):
    results: list[CheckResult]
    all_passed: bool


def output_result(result: BaseModel, as_json: bool) -> None:
    if as_json:
        print(result.model_dump_json(indent=2))
    else:
        for key, value in result.model_dump().items():
            typer.echo(f"{key}: {value}")


def find_base_branch() -> str:
    """Auto-detect the base branch by merge-base proximity to HEAD."""
    try:
        base, _, _ = detect_base_branch()
        return base
    except (typer.Exit, SystemExit):
        return get_integration_branch()


def status(
    branch: str | None,
    as_json: bool,
) -> None:
    """Fetch PR review status, checks, and comments for a branch."""
    branch_name = branch or current_branch()

    pr_list_raw = str(
        gh(
            "pr",
            "list",
            "--head",
            branch_name,
            "--state",
            "open",
            "--json",
            "number,title,url",
        )
    ).strip()
    prs = json.loads(pr_list_raw)

    if not prs:
        result = PRStatusResult(branch=branch_name, pr=None)
        output_result(result, as_json)
        if not as_json:
            typer.echo(f"No open PR found for branch {branch_name}")
        return

    pr_data = prs[0]
    pr_number = pr_data["number"]

    detail_raw = str(
        gh(
            "pr",
            "view",
            str(pr_number),
            "--json",
            "reviews,statusCheckRollup,mergeable,mergeStateStatus,reviewDecision",
        )
    ).strip()
    detail = json.loads(detail_raw)

    reviews = [
        ReviewInfo(
            author=r.get("author", {}).get("login", "unknown"),
            state=r.get("state", ""),
            body=r.get("body", ""),
        )
        for r in detail.get("reviews", [])
    ]

    raw_checks = detail.get("statusCheckRollup", []) or []
    checks = [
        CheckInfo(
            name=c.get("name", c.get("context", "unknown")),
            status=c.get("status", ""),
            conclusion=c.get("conclusion", ""),
        )
        for c in raw_checks
    ]

    checks_passing = (
        all(
            c.conclusion.upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")
            for c in checks
            if c.status.upper() == "COMPLETED"
        )
        if checks
        else True
    )

    pr_info = PRInfo(
        number=pr_number,
        title=pr_data["title"],
        url=pr_data["url"],
        review_decision=detail.get("reviewDecision", ""),
        mergeable=detail.get("mergeable", ""),
        checks_passing=checks_passing,
        reviews=reviews,
        checks=checks,
    )

    result = PRStatusResult(branch=branch_name, pr=pr_info)
    output_result(result, as_json)


def merge(
    pr_number: int,
    dry_run: bool,
) -> None:
    """Squash-merge a PR and pull changes into the integration branch."""
    integration = get_integration_branch()

    if dry_run:
        typer.echo(f"Would merge PR #{pr_number} (squash)")
        typer.echo(f"Would pull changes into {integration}")
        return

    try:
        gh("pr", "merge", str(pr_number), "--squash", "--delete-branch")
        typer.echo(f"Merged PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Merge failed: {stderr}", err=True)
        raise typer.Exit(1)

    tree_dir = Path.cwd().parent
    integration_path = tree_dir / integration
    pulled = False
    if integration_path.is_dir():
        try:
            git("-C", str(integration_path), "pull")
            typer.echo(f"Pulled changes into {integration}")
            pulled = True
        except sh.ErrorReturnCode as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            typer.echo(f"Warning: pull failed in {integration}: {stderr}", err=True)

    result = MergeResult(
        pr_number=pr_number,
        merged=True,
        integration_branch=integration,
        pulled=pulled,
    )
    print(result.model_dump_json(indent=2))


def sync_base(
    base: str | None,
    as_json: bool,
) -> None:
    """Sync the base branch and merge it into the current feature branch."""
    feature = current_branch()
    base_branch = base or find_base_branch()

    if not as_json:
        typer.echo(f"Feature branch: {feature}", err=True)
        typer.echo(f"Base branch: {base_branch}", err=True)

    tree_dir = Path.cwd().parent
    base_path = tree_dir / base_branch

    if base_path.is_dir():
        if not as_json:
            typer.echo(f"Syncing {base_branch}...", err=True)
        try:
            git("-C", str(base_path), "pull")
            git("-C", str(base_path), "push")
        except sh.ErrorReturnCode as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            typer.echo(f"Warning: sync of {base_branch} failed: {stderr}", err=True)

    if not as_json:
        typer.echo(f"Merging {base_branch} into {feature}...", err=True)

    try:
        git("merge", base_branch)
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            merged=True,
            conflicts=[],
        )
    except sh.ErrorReturnCode:
        conflict_output = str(
            git("diff", "--name-only", "--diff-filter=U", _ok_code=[0, 1])
        ).strip()
        conflicts = [f for f in conflict_output.splitlines() if f]
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            merged=False,
            conflicts=conflicts,
        )
        if not as_json:
            typer.echo(f"Merge conflicts in {len(conflicts)} file(s):", err=True)
            for f in conflicts:
                typer.echo(f"  {f}", err=True)

    output_result(result, as_json)
    if not result.merged:
        raise typer.Exit(1)


def push(
    force: bool,
    as_json: bool,
) -> None:
    """Push the current branch and report any existing PR."""
    branch_name = current_branch()

    try:
        if force:
            git("push", "--force")
        else:
            git("push", "-u", "origin", branch_name)
        pushed = True
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Push failed: {stderr}", err=True)
        pushed = False

    existing_pr = None
    try:
        pr_raw = str(
            gh(
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "open",
                "--json",
                "number,url",
            )
        ).strip()
        prs = json.loads(pr_raw) if pr_raw else []
        if prs:
            existing_pr = {"number": prs[0]["number"], "url": prs[0]["url"]}
    except sh.ErrorReturnCode:
        pass

    result = PushResult(
        branch=branch_name,
        pushed=pushed,
        force=force,
        existing_pr=existing_pr,
    )
    output_result(result, as_json)
    if not pushed:
        raise typer.Exit(1)


def create(
    base: str,
    title: str,
    body: str,
    as_json: bool,
) -> None:
    """Create a new PR."""
    try:
        raw = str(
            gh(
                "pr",
                "create",
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
                "--json",
                "number,url",
            )
        ).strip()
        data = json.loads(raw)
        result = CreateResult(number=data["number"], url=data["url"])
        output_result(result, as_json)
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Failed to create PR: {stderr}", err=True)
        raise typer.Exit(1)


def update(
    pr_number: int,
    body: str,
) -> None:
    """Update a PR body."""
    try:
        gh("pr", "edit", str(pr_number), "--body", body)
        typer.echo(f"Updated PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Failed to update PR: {stderr}", err=True)
        raise typer.Exit(1)


def checks(as_json: bool) -> None:
    """Run pyright, ruff, and pytest validation checks."""
    check_commands = [
        ("pyright", [sys.executable, "-m", "pyright"]),
        ("ruff-check", [sys.executable, "-m", "ruff", "check", "."]),
        ("ruff-format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        ("pytest", [sys.executable, "-m", "pytest"]),
    ]

    results: list[CheckResult] = []
    for name, cmd in check_commands:
        if not as_json:
            typer.echo(f"Running {name}...", err=True)
        try:
            output = str(uv("run", *cmd[1:], _ok_code=[0]))
            results.append(CheckResult(name=name, passed=True, output=output))
        except sh.ErrorReturnCode as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else str(e.stdout)
            output = (stdout + "\n" + stderr).strip()
            results.append(CheckResult(name=name, passed=False, output=output))

    all_passed = all(r.passed for r in results)
    final = ChecksResult(results=results, all_passed=all_passed)

    if as_json:
        print(final.model_dump_json(indent=2))
    else:
        for r in results:
            status_str = "PASS" if r.passed else "FAIL"
            typer.echo(f"  {r.name}: {status_str}")
            if not r.passed:
                for line in r.output.splitlines()[:10]:
                    typer.echo(f"    {line}")
        typer.echo(f"\n{'All checks passed' if all_passed else 'Some checks failed'}")

    if not all_passed:
        raise typer.Exit(1)
