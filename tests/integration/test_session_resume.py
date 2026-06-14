"""Native SDK session resume: a persisted run can be continued by its id.

Proves the foundation under lup's ``persist_session``/``resume`` plumbing —
that a query made with ``persist_session=True`` writes a transcript under the
chosen ``CLAUDE_CONFIG_DIR``, exposes its ``session_id``, and that a second
query with ``resume=<id>`` (and the same ``cwd``) sees the first turn's
context. Skips without an API key so it only runs where billing is intended.
"""

import os
from pathlib import Path

import pytest

from lup.client import client_env, query

pytestmark = pytest.mark.integration

CODEWORD = "GANYMEDE"
HAIKU = "claude-haiku-4-5-20251001"

needs_api_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY for a live, hermetic run",
)


@needs_api_key
async def test_persisted_session_resumes_prior_context(tmp_path: Path) -> None:
    config_dir = tmp_path / "claude-config"
    token = client_env.set({"CLAUDE_CONFIG_DIR": str(config_dir)})
    try:
        first = await query(
            f"Remember this codeword: {CODEWORD}. Reply with just 'ok'.",
            model=HAIKU,
            cwd=str(tmp_path),
            persist_session=True,
            max_turns=1,
        )
        session_id = first.session_id
        assert session_id is not None

        transcripts = list(config_dir.glob("projects/**/*.jsonl"))
        assert transcripts, "persist_session=True should write a transcript file"

        second = await query(
            "What was the codeword I gave you? Reply with just the word.",
            model=HAIKU,
            cwd=str(tmp_path),
            persist_session=True,
            resume=session_id,
            max_turns=1,
        )
        assert CODEWORD in (second.text or "").upper()
    finally:
        client_env.reset(token)
