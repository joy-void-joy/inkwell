"""Tests for conversation extraction — content block type handling.

The fixtures hand the models the wire JSON a share snapshot really carries,
so these cover the parsing as well as the reading: a block shape the API
sends that the schema does not declare fails here rather than reading as
empty.
"""

from collections.abc import Mapping

from inkwell.agent.tools.extract import ContentBlock, ShareMessage

type Wire = Mapping[str, object]


def block(raw: Wire) -> ContentBlock:
    return ContentBlock.model_validate(raw)


def message(raw: Wire) -> ShareMessage:
    return ShareMessage.model_validate(raw)


class TestBlockProse:
    def test_text_block(self) -> None:
        assert block({"type": "text", "text": "hello world"}).prose() == "hello world"

    def test_text_block_empty(self) -> None:
        assert block({"type": "text", "text": ""}).prose() is None

    def test_document_block_text_field(self) -> None:
        raw = {"type": "document", "text": "pasted content here"}
        assert block(raw).prose() == "pasted content here"

    def test_document_block_source_field(self) -> None:
        raw = {"type": "document", "source": "from source"}
        assert block(raw).prose() == "from source"

    def test_document_block_content_field(self) -> None:
        raw = {"type": "content", "content": "nested content"}
        assert block(raw).prose() == "nested content"

    def test_tool_result_with_nested_text(self) -> None:
        raw = {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "line one"},
                {"type": "text", "text": "line two"},
            ],
        }
        assert block(raw).prose() == "line one\n\nline two"

    def test_tool_result_with_string_content(self) -> None:
        raw = {"type": "tool_result", "content": "plain string"}
        assert block(raw).prose() == "plain string"

    def test_tool_result_empty_nested(self) -> None:
        assert block({"type": "tool_result", "content": []}).prose() is None

    def test_image_block_skipped(self) -> None:
        assert block({"type": "image", "source": ""}).prose() is None

    def test_unknown_type_with_text_field(self) -> None:
        assert block({"type": "attachment", "text": "recovered"}).prose() == "recovered"

    def test_unknown_type_no_text(self) -> None:
        assert block({"type": "unknown", "data": 42}).prose() is None


class TestMessageText:
    def test_mixed_content_blocks(self) -> None:
        result = message(
            {
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "document", "text": "Pasted document content"},
                    {"type": "image", "source": ""},
                    {"type": "text", "text": "Goodbye"},
                ]
            }
        ).text()
        assert "Hello" in result
        assert "Pasted document content" in result
        assert "Goodbye" in result
        assert result.count("\n\n") == 2

    def test_string_content_fallback(self) -> None:
        assert message({"content": "just a string"}).text() == "just a string"

    def test_empty_message(self) -> None:
        assert message({"content": []}).text() == ""

    def test_non_dict_blocks_skipped(self) -> None:
        """One unreadable block costs that block, not the rest of the message."""
        raw = {"content": ["not a block", {"type": "text", "text": "ok"}]}
        assert message(raw).text() == "ok"

    def test_attachments_follow_the_blocks(self) -> None:
        raw = {
            "content": [{"type": "text", "text": "see attached"}],
            "attachments": [{"file_name": "notes.md", "extracted_content": "body"}],
        }
        assert message(raw).text() == "see attached\n\n[Attachment: notes.md]\nbody"
