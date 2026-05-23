"""Worktree create, list, and remove operations."""

import shutil
from pathlib import Path

import typer


PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache" / "local" / "lup"

GITIGNORED_EXTRAS = [
    ".env.local",
    "downstream.json.local",
    ".claude/settings.local.json",
    "logs",
    "refs",
]


def branch_exists(branch: str) -> bool:
    """Check if a git branch exists (local only)."""
    import sh

    git = sh.Command("git")
    try:
        git("rev-parse", "--verify", f"refs/heads/{branch}")
        return True
    except sh.ErrorReturnCode:
        return False


def worktree_is_registered(path: Path) -> bool:
    """Check if a path is registered as a git worktree (even if dir is missing)."""
    import sh

    git = sh.Command("git")
    output = str(git("worktree", "list", "--porcelain"))
    resolved = str(path.resolve())
    for line in output.splitlines():
        if line.startswith("worktree ") and line.split(" ", 1)[1] == resolved:
            return True
    return False


def get_tree_dir() -> Path:
    """Find the tree/ directory that contains worktrees."""
    cwd = Path.cwd().resolve()

    if cwd.parent.name == "tree":
        return cwd.parent

    tree = cwd / "tree"
    if tree.is_dir():
        return tree

    for parent in cwd.parents:
        tree = parent / "tree"
        if tree.is_dir():
            return tree

    typer.echo("Error: Could not find tree/ directory", err=True)
    raise typer.Exit(1)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    import sh

    try:
        xclip = sh.Command("xclip")
        xclip("-selection", "clipboard", _in=text)
        return True
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        pass
    try:
        xsel = sh.Command("xsel")
        xsel("--clipboard", "--input", _in=text)
        return True
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        pass
    return False


def create(
    name: str,
    no_sync: bool,
    no_copy_data: bool,
    no_plugin_refresh: bool,
    base_branch: str | None,
) -> None:
    """Create or re-attach a git worktree."""
    import sh

    git = sh.Command("git")
    uv = sh.Command("uv")
    current_dir = Path.cwd()

    tree_dir = get_tree_dir()
    worktree_path = tree_dir / name
    already_exists = branch_exists(name)

    if worktree_path.exists():
        if worktree_is_registered(worktree_path):
            typer.echo(f"Worktree already active: {worktree_path}")
            raise typer.Exit(0)
        typer.echo(f"Removing stale worktree directory: {worktree_path}")
        shutil.rmtree(worktree_path)

    git("worktree", "prune")

    if already_exists:
        typer.echo(f"Re-attaching worktree: {worktree_path}")
        typer.echo(f"Existing branch: {name}")
    else:
        typer.echo(f"Creating worktree: {worktree_path}")
        typer.echo(f"New branch: {name}")

    try:
        if already_exists:
            git("worktree", "add", str(worktree_path), name)
        elif base_branch:
            git("worktree", "add", str(worktree_path), "-b", name, base_branch)
        else:
            git("worktree", "add", str(worktree_path), "-b", name)
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Error creating worktree: {stderr}")
        raise typer.Exit(1)

    if not no_copy_data:
        for rel_path in GITIGNORED_EXTRAS:
            src = current_dir / rel_path
            if not src.exists():
                continue
            dst = worktree_path / rel_path
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
                typer.echo(f"Copied {rel_path}/")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                typer.echo(f"Copied {rel_path}")

    if not no_sync:
        typer.echo("Running uv sync...")
        try:
            uv("sync", _cwd=str(worktree_path))
        except sh.ErrorReturnCode as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            typer.echo(f"Warning: uv sync failed: {stderr}")

    if not no_plugin_refresh:
        if PLUGIN_CACHE_DIR.exists():
            shutil.rmtree(PLUGIN_CACHE_DIR)
            typer.echo("Cleared plugin cache (lup)")

        claude = sh.Command("claude")
        try:
            claude(
                "plugin",
                "install",
                "lup@local",
                "--scope",
                "project",
                _cwd=str(worktree_path),
                _tty_out=False,
            )
            typer.echo("Installed lup plugin (project scope)")
        except sh.ErrorReturnCode as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            typer.echo(f"Warning: plugin install failed: {stderr}", err=True)

    typer.echo()
    cd_command = f"cd /; cd {worktree_path}; claude"

    if copy_to_clipboard(cd_command):
        typer.echo(f"Copied to clipboard: {cd_command}")
    else:
        typer.echo("Done! To switch to the new worktree:")
        typer.echo(f"  {cd_command}")


def list_worktrees() -> None:
    """List all git worktrees with branch and status info."""
    import sh

    git = sh.Command("git")
    output = str(git("worktree", "list", "--porcelain"))

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line.split(" ", 1)[1]
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1][:10]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
        elif line == "bare":
            current["bare"] = "true"
        elif line == "prunable":
            current["prunable"] = "true"

    if current:
        entries.append(current)

    if not entries:
        typer.echo("No worktrees found")
        return

    typer.echo(f"\n=== Worktrees ({len(entries)}) ===\n")
    typer.echo(f"{'Branch':<30} {'HEAD':<12} {'Path'}")
    typer.echo("-" * 80)

    for entry in entries:
        branch = entry.get("branch", "(bare)" if entry.get("bare") else "(detached)")
        head = entry.get("head", "")
        path = entry.get("path", "")
        flags = ""
        if entry.get("prunable"):
            flags = " [prunable]"
        typer.echo(f"{branch:<30} {head:<12} {path}{flags}")


def remove(name: str, force: bool) -> None:
    """Remove a git worktree."""
    import sh

    git = sh.Command("git")
    path = Path(name)

    if not path.is_absolute():
        tree_dir = get_tree_dir()
        path = tree_dir / name

    if not worktree_is_registered(path):
        typer.echo(f"Not a registered worktree: {path}", err=True)
        raise typer.Exit(1)

    try:
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        git(*args)
        typer.echo(f"Removed worktree: {path}")
    except sh.ErrorReturnCode as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
        typer.echo(f"Error removing worktree: {stderr}", err=True)
        if not force:
            typer.echo("Use --force to remove even if dirty")
        raise typer.Exit(1)
