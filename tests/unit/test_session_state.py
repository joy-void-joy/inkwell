"""Tests for WritingSessionState."""

from inkwell.agent.session import WritingSessionState


class TestWritingSessionState:
    def test_initial_state(self) -> None:
        state = WritingSessionState()
        assert state.doc_id == ""
        assert state.stage == "starting"
        assert state.sections == []
        assert state.pending_questions == []

    def test_set_doc(self) -> None:
        state = WritingSessionState()
        state.set_doc("doc123", "https://docs.google.com/doc123")
        assert state.doc_id == "doc123"
        assert state.doc_url == "https://docs.google.com/doc123"

    def test_add_and_update_section(self) -> None:
        state = WritingSessionState()
        state.add_section("Introduction", "tab1")
        state.add_section("Methods", "tab2")

        assert len(state.sections) == 2
        assert state.sections[0]["status"] == "planned"

        state.update_section_status("Introduction", "writing")
        assert state.sections[0]["status"] == "writing"

    def test_update_nonexistent_section(self) -> None:
        state = WritingSessionState()
        state.add_section("Intro", "tab1")
        state.update_section_status("Nonexistent", "writing")
        assert state.sections[0]["status"] == "planned"

    def test_pending_questions(self) -> None:
        state = WritingSessionState()
        state.add_question("What about X?")
        state.add_question("How about Y?")
        assert len(state.pending_questions) == 2

    def test_agent_comment_tracking(self) -> None:
        state = WritingSessionState()
        state.mark_agent_comment("c1")
        state.mark_agent_comment("c2")
        assert "c1" in state.agent_comment_ids
        assert "c2" in state.agent_comment_ids

    def test_mark_comments_seen(self) -> None:
        state = WritingSessionState()
        state.mark_comments_seen(["c1", "c2", "c3"])
        assert state.seen_comment_ids == {"c1", "c2", "c3"}

    async def test_check_unread_no_doc(self) -> None:
        state = WritingSessionState()
        assert await state.check_unread_comments() == 0

    def test_set_stage(self) -> None:
        state = WritingSessionState()
        state.set_stage("writing")
        assert state.stage == "writing"
        state.set_stage("reviewing")
        assert state.stage == "reviewing"
