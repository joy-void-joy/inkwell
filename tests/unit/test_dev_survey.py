"""``parse_branches`` reads git state without terminal decorations.

The branch survey once parsed ``git branch -vv``, whose output carries ``*``/``+``
markers for the current and worktree-checked-out branches and (under a TTY or
``color.ui=always``) ANSI color escapes. Those decorations leaked into branch
names, which then failed every downstream ``rev-list``/``diff`` containment
query and surfaced as ``-1`` sentinels. This pins the decoration-free contract
against a real repository whose worktree branch would otherwise be marked ``+``.
"""

from pathlib import Path

import pytest
import sh

from inkwell.devtools.dev.branches import parse_branches


def init_repo(path: Path) -> sh.Command:
    """Initialize a one-commit repo with color forced on; return a baked git."""
    git = sh.Command("git").bake("-C", str(path), _tty_out=False)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "color.ui", "always")
    (path / "file.txt").write_text("hello\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return git


def test_parse_branches_ignores_decorations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = init_repo(repo)
    git("branch", "feature")
    git("branch", "--set-upstream-to=main", "feature")
    git("worktree", "add", "-q", str(tmp_path / "wt"), "feature")

    monkeypatch.chdir(repo)
    by_name = {str(b["name"]): b for b in parse_branches()}

    assert set(by_name) == {"main", "feature"}
    for name in by_name:
        assert "\x1b" not in name and name == name.strip()
    assert by_name["main"]["is_current"] is True
    assert by_name["feature"]["is_current"] is False
    assert by_name["feature"]["tracking"] == "main"
    assert by_name["main"]["tracking"] == ""
