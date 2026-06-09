"""Tests for extract_json_object — the text fallback for structured output."""

from lup.client import extract_json_object


class TestExtractJsonObject:
    def test_fenced_code_block(self) -> None:
        text = 'Some preamble\n```json\n{"a": 1}\n```\ntrailing'
        assert extract_json_object(text) == {"a": 1}

    def test_bare_json(self) -> None:
        text = 'Here is the result: {"key": "value", "n": 2}'
        assert extract_json_object(text) == {"key": "value", "n": 2}

    def test_nested_braces(self) -> None:
        text = '{"outer": {"inner": true}}'
        assert extract_json_object(text) == {"outer": {"inner": True}}

    def test_no_json(self) -> None:
        assert extract_json_object("just some text") is None

    def test_empty_string(self) -> None:
        assert extract_json_object("") is None

    def test_malformed_json_in_fence(self) -> None:
        text = '```json\n{not valid json}\n```'
        assert extract_json_object(text) is None

    def test_prefers_fence_over_bare(self) -> None:
        text = '{"wrong": 1}\n```json\n{"right": 2}\n```'
        assert extract_json_object(text) == {"right": 2}

    def test_complex_nested_structure(self) -> None:
        text = (
            "Thinking...\n```json\n"
            '{"plan": {"title": "T", "sections": [{"s": 1}]}, "ok": true}\n'
            "```"
        )
        result = extract_json_object(text)
        assert result is not None
        assert result["ok"] is True  # type: ignore[index]
        assert result["plan"]["sections"][0]["s"] == 1  # type: ignore[index]

    def test_fence_without_closing(self) -> None:
        text = '```json\n{"a": 1}\nno closing fence'
        result = extract_json_object(text)
        assert result == {"a": 1}

    def test_only_opening_brace(self) -> None:
        assert extract_json_object("just { here") is None
