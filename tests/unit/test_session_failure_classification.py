"""A pipeline interrupt is resumable, not a crash — the web layer must say so.

An interrupted merge (a reloaded dev server killing the subprocess, or a resume
that stopped before any output) surfaced to the UI as a hard failure carrying
the misleading "Pipeline completed without producing output". The classifier
routes both interrupt shapes — a resume's ``PipelineInterrupted`` and a raw
SIGINT (``exit code -2``) — to a resumable status instead.
"""

from claude_agent_sdk import ProcessError

from inkwell.agent.pipeline import PipelineInterrupted
from inkwell.environment.web.session_manager import classify_session_failure
from inkwell.agent.client import is_interrupt


def test_resume_interrupt_is_resumable_and_names_stage() -> None:
    failure = classify_session_failure(PipelineInterrupted("merge"))
    assert failure.status == "interrupted"
    assert "merge" in failure.message


def test_sigint_subprocess_is_resumable() -> None:
    exc = ProcessError("Command failed with exit code -2", exit_code=-2, stderr="")
    assert classify_session_failure(exc).status == "interrupted"


def test_genuine_crash_is_a_failure() -> None:
    failure = classify_session_failure(ValueError("boom"))
    assert failure.status == "failed"
    assert "boom" in failure.message


def test_interrupted_exception_is_not_mistaken_for_a_sigint() -> None:
    # The two interrupt paths must stay distinct: a resume's PipelineInterrupted
    # is detected by isinstance, never by is_interrupt. Its message must not
    # carry an "exit code -2" token, or the detection paths would conflate.
    assert is_interrupt(PipelineInterrupted("merge")) is False
