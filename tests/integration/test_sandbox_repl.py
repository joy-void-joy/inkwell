"""Integration tests for the persistent REPL sandbox.

Requires Docker to be running. Tests exercise Sandbox.run_code() and
Sandbox.run_install() directly — no LLM involved.
"""

from pathlib import Path

import pytest

from lup.sandbox import Sandbox

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    """Create a sandbox with pre-install disabled for speed."""
    return Sandbox(
        session_id="test-repl-integration",
        shared_dir=tmp_path / "shared",
        pre_install=None,
    )


class TestReplPersistence:
    """Variables and imports persist across execute_code calls."""

    def test_variable_persists(self, sandbox: Sandbox) -> None:
        with sandbox:
            r1 = sandbox.run_code("x = 42\nprint(x)")
            assert r1["exit_code"] == 0
            assert "42" in r1["stdout"]

            r2 = sandbox.run_code("print(x + 1)")
            assert r2["exit_code"] == 0
            assert "43" in r2["stdout"]

    def test_import_persists(self, sandbox: Sandbox) -> None:
        with sandbox:
            r1 = sandbox.run_code("import json")
            assert r1["exit_code"] == 0

            r2 = sandbox.run_code('print(json.dumps({"a": 1}))')
            assert r2["exit_code"] == 0
            assert '{"a": 1}' in r2["stdout"]

    def test_multiline_computation(self, sandbox: Sandbox) -> None:
        """Multi-step computation with numpy."""
        with sandbox:
            sandbox.run_install(["numpy"])
            r1 = sandbox.run_code(
                "import numpy as np\n"
                "M = np.random.default_rng(0).random((5, 5))\n"
                "print('shape:', M.shape)"
            )
            assert r1["exit_code"] == 0
            assert "(5, 5)" in r1["stdout"]

            r2 = sandbox.run_code(
                "eigvals = np.linalg.eigvals(M)\nprint('count:', len(eigvals))"
            )
            assert r2["exit_code"] == 0
            assert "count: 5" in r2["stdout"]

            r3 = sandbox.run_code(
                "trace = np.trace(M)\n"
                "eigsum = eigvals.sum().real\n"
                "print(f'match: {abs(trace - eigsum) < 1e-10}')"
            )
            assert r3["exit_code"] == 0
            assert "match: True" in r3["stdout"]


class TestInstallPackage:
    """install_package makes packages available to the REPL."""

    def test_install_and_import(self, sandbox: Sandbox) -> None:
        """Install a package, then import it in the existing REPL."""
        with sandbox:
            r1 = sandbox.run_code(
                "data = [['Alice', 90], ['Bob', 85]]\nprint(len(data))"
            )
            assert r1["exit_code"] == 0
            assert "2" in r1["stdout"]

            r2 = sandbox.run_code("from tabulate import tabulate")
            assert r2["exit_code"] != 0, "tabulate should not be available yet"

            install = sandbox.run_install(["tabulate"])
            assert install["exit_code"] == 0

            r3 = sandbox.run_code(
                "from tabulate import tabulate\n"
                "print(tabulate(data, headers=['Name', 'Score']))"
            )
            assert r3["exit_code"] == 0
            assert "Alice" in r3["stdout"]

    def test_state_survives_error(self, sandbox: Sandbox) -> None:
        """Variables defined before an error still exist after it."""
        with sandbox:
            sandbox.run_code("keeper = 'still here'")

            r_err = sandbox.run_code("raise ValueError('boom')")
            assert r_err["exit_code"] != 0

            r_ok = sandbox.run_code("print(keeper)")
            assert r_ok["exit_code"] == 0
            assert "still here" in r_ok["stdout"]
