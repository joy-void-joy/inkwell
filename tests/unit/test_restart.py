"""Rewind-point resolution for restarting a stage from scratch.

``checkpoint_before`` decides which saved snapshot a restart rewinds to: the
latest one strictly before the stage being redone, skipping stages that never
checkpointed. That choice is what makes a restart regenerate from clean
upstream state with fresh agents, so it is exercised against on-disk files.
"""

import lup.workspace.paths as paths
import pytest

from inkwell.agent.core import checkpoint_before


def make_checkpoints(base, stages):
    for stage in stages:
        (base / f"snapshot_{stage}.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state", paths.PathState())
    paths.configure(notes_dir=tmp_path)
    session_id = "session-under-test"
    base = paths.sessions_dir() / session_id / "pipeline_notes"
    base.mkdir(parents=True)
    return session_id, base


def test_rewinds_to_the_immediate_predecessor(session_dir):
    session_id, base = session_dir
    make_checkpoints(
        base,
        ["preprocess", "extract", "voice", "plan", "research", "assumptions", "refine"],
    )
    assert checkpoint_before(session_id, "write") == "refine"


def test_skips_stages_that_never_checkpointed(session_dir):
    # resolve runs but never saves a snapshot, so redoing rewrite lands on review.
    session_id, base = session_dir
    make_checkpoints(
        base,
        [
            "preprocess",
            "extract",
            "voice",
            "plan",
            "research",
            "assumptions",
            "refine",
            "write",
            "merge",
            "review",
        ],
    )
    assert checkpoint_before(session_id, "rewrite") == "review"


def test_excludes_the_stage_being_redone(session_dir):
    # Redoing research must rewind to plan, not to research's own checkpoint.
    session_id, base = session_dir
    make_checkpoints(base, ["preprocess", "extract", "voice", "plan", "research"])
    assert checkpoint_before(session_id, "research") == "plan"


def test_none_when_nothing_earlier_was_checkpointed(session_dir):
    session_id, base = session_dir
    make_checkpoints(base, ["extract"])
    assert checkpoint_before(session_id, "extract") is None


def test_none_for_an_unknown_stage(session_dir):
    session_id, base = session_dir
    make_checkpoints(base, ["plan"])
    assert checkpoint_before(session_id, "bogus") is None
