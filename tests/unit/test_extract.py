"""Tests for conversation extraction — content block type handling."""

from inkwell.agent.tools.extract import extract_block_text, message_text


class TestExtractBlockText:
    def test_text_block(self) -> None:
        block: dict[str, object] = {"type": "text", "text": "hello world"}
        assert extract_block_text(block) == "hello world"

    def test_text_block_empty(self) -> None:
        block: dict[str, object] = {"type": "text", "text": ""}
        assert extract_block_text(block) is None

    def test_document_block_text_field(self) -> None:
        block: dict[str, object] = {"type": "document", "text": "pasted content here"}
        assert extract_block_text(block) == "pasted content here"

    def test_document_block_source_field(self) -> None:
        block: dict[str, object] = {"type": "document", "source": "from source"}
        assert extract_block_text(block) == "from source"

    def test_document_block_content_field(self) -> None:
        block: dict[str, object] = {"type": "content", "content": "nested content"}
        assert extract_block_text(block) == "nested content"

    def test_tool_result_with_nested_text(self) -> None:
        block: dict[str, object] = {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "line one"},
                {"type": "text", "text": "line two"},
            ],
        }
        assert extract_block_text(block) == "line one\n\nline two"

    def test_tool_result_with_string_content(self) -> None:
        block: dict[str, object] = {"type": "tool_result", "content": "plain string"}
        assert extract_block_text(block) == "plain string"

    def test_tool_result_empty_nested(self) -> None:
        block: dict[str, object] = {"type": "tool_result", "content": []}
        assert extract_block_text(block) is None

    def test_image_block_skipped(self) -> None:
        block: dict[str, object] = {"type": "image", "source": {"data": "..."}}
        assert extract_block_text(block) is None

    def test_unknown_type_with_text_field(self) -> None:
        block: dict[str, object] = {"type": "attachment", "text": "recovered"}
        assert extract_block_text(block) == "recovered"

    def test_unknown_type_no_text(self) -> None:
        block: dict[str, object] = {"type": "unknown", "data": 42}
        assert extract_block_text(block) is None


class TestMessageText:
    def test_mixed_content_blocks(self) -> None:
        msg: dict[str, object] = {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "document", "text": "Pasted document content"},
                {"type": "image", "source": {"data": "..."}},
                {"type": "text", "text": "Goodbye"},
            ]
        }
        result = message_text(msg)
        assert "Hello" in result
        assert "Pasted document content" in result
        assert "Goodbye" in result
        assert result.count("\n\n") == 2

    def test_string_content_fallback(self) -> None:
        msg: dict[str, object] = {"content": "just a string"}
        assert message_text(msg) == "just a string"

    def test_empty_message(self) -> None:
        msg: dict[str, object] = {"content": []}
        assert message_text(msg) == ""

    def test_non_dict_blocks_skipped(self) -> None:
        msg: dict[str, object] = {
            "content": ["not a dict", {"type": "text", "text": "ok"}]
        }
        assert message_text(msg) == "ok"
