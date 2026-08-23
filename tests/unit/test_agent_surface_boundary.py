"""Architecture gate for the model-session observation boundary."""

import ast
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_MODULE = PROJECT_ROOT / "src" / "inkwell" / "agent" / "client.py"
RAW_FACTORY_SYMBOLS = frozenset(
    {
        "create_claude_session_factory",
        "decorated_session_factory",
        "observed_factory",
        "observing_factory",
        "open_session",
        "provider_factory",
    }
)


def raw_session_violations(source: str) -> tuple[str, ...]:
    """Return every syntax node that bypasses ``AgentSurface``."""
    tree = ast.parse(source)
    violations: list[str] = []
    aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for imported in node.names:
                local = imported.asname or imported.name
                aliases[local] = imported.name
                if imported.name in RAW_FACTORY_SYMBOLS:
                    violations.append(f"line {node.lineno}: imports {imported.name}")
            if module.startswith("lup.adapters.") and (
                ".runtime" in module or ".selection" in module
            ):
                violations.append(f"line {node.lineno}: imports provider runtime")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            resolved = aliases[node.id] if node.id in aliases else node.id
            if resolved in RAW_FACTORY_SYMBOLS:
                violations.append(f"line {node.lineno}: uses {resolved}")
        if isinstance(node, ast.Attribute) and node.attr in RAW_FACTORY_SYMBOLS:
            violations.append(f"line {node.lineno}: uses {node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and (aliases[node.func.id] if node.func.id in aliases else node.func.id)
            == "SessionFactory"
        ):
            violations.append(f"line {node.lineno}: constructs SessionFactory")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "session_factory"
            and (
                (
                    isinstance(node.func.value, ast.Name)
                    and (
                        aliases[node.func.value.id]
                        if node.func.value.id in aliases
                        else node.func.value.id
                    )
                    == "RUNTIME"
                )
                or (
                    isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "RUNTIME"
                )
            )
        ):
            violations.append(f"line {node.lineno}: opens a raw runtime session")

    return tuple(dict.fromkeys(violations))


@pytest.mark.parametrize(
    "source",
    [
        "from inkwell.agent.client import provider_factory\nprovider_factory()",
        "import inkwell.agent.client as client\nclient.observed_factory(factory)",
        "from lup.adapters.claude.runtime import create_claude_session_factory",
        "from lup.runtime.factory import SessionFactory\nSessionFactory(open_session)",
        "from inkwell.agent.client import RUNTIME as runtime\nruntime.session_factory(request)",
    ],
)
def test_gate_rejects_raw_session_construction(source: str) -> None:
    assert raw_session_violations(source)


def test_gate_accepts_the_owning_surface() -> None:
    source = (
        "from inkwell.agent.client import AgentSurface\n"
        'factory = AgentSurface().session_factory(prefix="[test] ")'
    )

    assert raw_session_violations(source) == ()


def test_production_sessions_open_only_through_agent_surface() -> None:
    source_root = PROJECT_ROOT / "src" / "inkwell"
    violations: list[str] = []

    for path in sorted(source_root.rglob("*.py")):
        if path == CLIENT_MODULE:
            continue
        for violation in raw_session_violations(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: {violation}")

    assert violations == []
