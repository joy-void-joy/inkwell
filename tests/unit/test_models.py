"""Tests for output models."""

from lup_template.agent.models import AgentOutput


class TestOutputSchema:
    """Tests for JSON schema generation."""

    def test_schema_has_required_fields(self) -> None:
        """Schema should include required fields."""
        schema = AgentOutput.model_json_schema()

        assert "properties" in schema
        assert "summary" in schema["properties"]
        assert "confidence" in schema["properties"]
