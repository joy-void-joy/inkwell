"""Tests for agent-comment filtering — the self-feedback round-trip guard."""

from pathlib import Path

from lup.types import JsonValue

from inkwell.agent.session import WritingSessionState


def api_result(comments: list[JsonValue]) -> JsonValue:
    return {"comments": comments}


class TestParseComments:
    def test_agent_comment_without_reply_is_filtered(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")

        parsed = state.parse_comments(
            api_result([{"id": "c1", "content": "[STYLE] cut filler", "replies": []}])
        )

        assert parsed == []
        assert "c1" not in state.seen_comments

    def test_agent_comment_with_agent_only_reply_is_filtered(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")
        state.mark_agent_comment("r1")

        parsed = state.parse_comments(
            api_result(
                [
                    {
                        "id": "c1",
                        "content": "[QUESTION] msd or lsd?",
                        "replies": [{"id": "r1", "content": "Noted."}],
                    }
                ]
            )
        )

        assert parsed == []

    def test_agent_question_with_author_reply_surfaces_author_text(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")
        state.mark_agent_comment("r1")

        parsed = state.parse_comments(
            api_result(
                [
                    {
                        "id": "c1",
                        "content": "[QUESTION] msd or lsd?",
                        "replies": [
                            {"id": "r1", "content": "Noted."},
                            {"id": "r2", "content": "lsd-first, see chapter 2"},
                        ],
                    }
                ]
            )
        )

        assert len(parsed) == 1
        assert parsed[0]["reply"] == "lsd-first, see chapter 2"

    def test_author_comment_surfaces(self) -> None:
        state = WritingSessionState()

        parsed = state.parse_comments(
            api_result([{"id": "c9", "content": "wrong convention", "replies": []}])
        )

        assert len(parsed) == 1
        assert parsed[0]["content"] == "wrong convention"


class TestAgentIdsRegistry:
    def test_registry_survives_process_restart(self, tmp_path: Path) -> None:
        registry = tmp_path / "agent_ids.txt"

        first = WritingSessionState()
        first.attach_agent_ids_registry(registry)
        first.mark_agent_comment("c1")
        first.mark_agent_comment("r1")

        second = WritingSessionState()
        second.attach_agent_ids_registry(registry)

        assert "c1" in second.agent_comments
        assert "r1" in second.agent_comments

    def test_stale_snapshot_merges_with_registry(self, tmp_path: Path) -> None:
        registry = tmp_path / "agent_ids.txt"
        first = WritingSessionState()
        first.attach_agent_ids_registry(registry)
        first.mark_agent_comment("review-comment")

        resumed = WritingSessionState()
        resumed.attach_agent_ids_registry(registry)
        resumed.agent_comments.mark("older-snapshot-id")

        assert "review-comment" in resumed.agent_comments
        assert "older-snapshot-id" in resumed.agent_comments
