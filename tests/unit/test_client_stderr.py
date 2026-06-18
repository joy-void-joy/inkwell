"""Captured CLI stderr must surface on failure without breaking interrupt detection.

The Agent SDK raises ``ProcessError`` with a fixed ``"Check stderr output for
details"`` placeholder and never attaches the subprocess's real stderr — so an
``exit code 1`` crash (a merge subprocess dying) is undiagnosable. Splicing the
captured lines back in fixes that, but it must leave the ``exit code N`` token
intact, since ``is_interrupt`` keys off ``exit code -2``.
"""

from collections import deque

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ProcessError

from lup.client import attach_cli_stderr, build_client, is_interrupt

captured: dict[str, ClaudeAgentOptions] = {}


class FakeClient:
    def __init__(self, *, options: ClaudeAgentOptions) -> None:
        captured["options"] = options

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def make_process_error(exit_code: int) -> ProcessError:
    return ProcessError(
        f"Command failed with exit code {exit_code}",
        exit_code=exit_code,
        stderr="Check stderr output for details",
    )


def test_captured_stderr_replaces_placeholder() -> None:
    exc = make_process_error(1)
    attach_cli_stderr(
        exc, deque(["ModuleNotFoundError: no module named 'x'", "  at f"])
    )
    assert "ModuleNotFoundError" in str(exc)
    assert "Check stderr output for details" not in str(exc)
    assert exc.stderr == "ModuleNotFoundError: no module named 'x'\n  at f"


def test_enrichment_preserves_interrupt_detection() -> None:
    exc = make_process_error(-2)
    assert is_interrupt(exc)
    attach_cli_stderr(exc, deque(["KeyboardInterrupt"]))
    assert is_interrupt(exc)


def test_empty_buffer_leaves_error_untouched() -> None:
    exc = make_process_error(1)
    before = str(exc)
    attach_cli_stderr(exc, deque())
    assert str(exc) == before


async def test_build_client_enriches_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured.clear()
    monkeypatch.setattr("lup.client.ClaudeSDKClient", FakeClient)
    with pytest.raises(ProcessError) as excinfo:
        async with build_client(model="m"):
            sink = captured["options"].stderr
            assert sink is not None
            sink("RuntimeError: merge died")
            raise make_process_error(1)
    assert "RuntimeError: merge died" in str(excinfo.value)
