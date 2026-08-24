"""The web launcher owns the frontend build prerequisites."""

from collections.abc import Callable
from pathlib import Path

import pytest

import inkwell.environment.web.__main__ as web_main


@pytest.mark.parametrize("dependencies_present", [False, True])
def test_build_frontend_bootstraps_missing_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependencies_present: bool,
) -> None:
    (tmp_path / "package.json").write_text("{}")
    tool = tmp_path / "node_modules" / ".bin" / "tsc"
    if dependencies_present:
        tool.parent.mkdir(parents=True)
        tool.touch()

    calls: list[tuple[str, ...]] = []

    def command(name: str) -> Callable[..., None]:
        assert name == "npm"

        def record(*args: str, **_kwargs: str) -> None:
            calls.append(args)

        return record

    monkeypatch.setattr(web_main, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(web_main.sh, "Command", command)
    web_main.build_frontend()

    expected: list[tuple[str, ...]] = [("run", "build")]
    if not dependencies_present:
        expected.insert(0, ("ci",))
    assert calls == expected
