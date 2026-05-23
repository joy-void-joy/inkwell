"""Tests for ToolPolicy class."""

from lup_template.agent.config import settings
from lup_template.agent.tool_policy import ToolPolicy


class TestToolPolicyIsToolAvailable:
    """Tests for is_tool_available method."""

    def test_unknown_tool_available(self) -> None:
        """Unknown tools should be available (not excluded)."""
        policy = ToolPolicy(settings)

        assert policy.is_tool_available("mcp__custom__my_tool")
