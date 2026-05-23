"""Tests for output models."""

from inkwell.agent.models import WritingOutput


class TestOutputSchema:
    """Tests for JSON schema generation."""

    def test_schema_has_required_fields(self) -> None:
        schema = WritingOutput.model_json_schema()

        assert "properties" in schema
        assert "title" in schema["properties"]
        assert "google_doc_url" in schema["properties"]
        assert "word_count" in schema["properties"]
        assert "summary" in schema["properties"]
