"""Session resume: a run can be continued by the id its turn reported.

Proves the foundation under a resumed pipeline stage — that a turn reports the
provider session it ran on, that the transcript lands under the chosen
``CLAUDE_CONFIG_DIR``, and that a second turn resuming that id sees the first
one's context. Skips without an API key so it only runs where billing is
intended.
"""

import os
from pathlib import Path

import pytest

from inkwell.agent.client import client_env, query, result_text

pytestmark = pytest.mark.integration

CODEWORD = "GANYMEDE"
HAIKU = "claude-haiku-4-5-20251001"

needs_api_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY for a live, hermetic run",
)


@needs_api_key
async def test_a_resumed_session_carries_the_first_turn_context(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "claude-config"
    token = client_env.set({"CLAUDE_CONFIG_DIR": str(config_dir)})
    try:
        first = await query(
            f"Remember this codeword: {CODEWORD}. Reply with just 'ok'.",
            model=HAIKU,
            cwd=tmp_path,
            max_turns=1,
        )
        session_id = first.identifiers.session.value
        assert session_id

        transcripts = list(config_dir.glob("projects/**/*.jsonl"))
        assert transcripts, "a completed turn should leave a transcript file"

        second = await query(
            "What was the codeword I gave you? Reply with just the word.",
            model=HAIKU,
            cwd=tmp_path,
            resume=session_id,
            max_turns=1,
        )
        assert CODEWORD in result_text(second).upper()
    finally:
        client_env.reset(token)
