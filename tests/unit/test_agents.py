"""Tests for pipeline stage prompts and tool list consistency."""

from inkwell.agent.stages import (
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
    PLANNER_SYSTEM,
    RECONCILE_PROMPT,
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    SECTION_WRITER_PROMPT,
    STYLE_REVIEWER_PROMPT,
)
from inkwell.agent.tool_policy import (
    RESEARCH_TOOLS,
    research_tool_names,
    review_tool_names,
)


ALL_PROMPTS = {
    "planner": PLANNER_SYSTEM,
    "researcher": RESEARCHER_PROMPT,
    "section_writer": SECTION_WRITER_PROMPT,
    "reconcile": RECONCILE_PROMPT,
    "narrative_reviewer": NARRATIVE_REVIEWER_PROMPT,
    "fact_checker": FACT_CHECKER_PROMPT,
    "style_reviewer": STYLE_REVIEWER_PROMPT,
    "rewriter": REWRITER_SYSTEM,
}


class TestStagePrompts:
    def test_all_prompts_non_empty(self) -> None:
        for name, prompt in ALL_PROMPTS.items():
            assert prompt, f"{name} has no prompt"
            assert len(prompt) > 50, f"{name} prompt is too short"

    def test_prompts_have_output_section(self) -> None:
        prompts_with_output = {
            "researcher",
            "section_writer",
            "reconcile",
            "narrative_reviewer",
            "fact_checker",
            "style_reviewer",
        }
        for name, prompt in ALL_PROMPTS.items():
            if name in prompts_with_output:
                assert "Output" in prompt or "output" in prompt, (
                    f"{name} prompt has no output specification"
                )


class TestToolListConsistency:
    def test_research_tools_match_policy(self) -> None:
        rt = set(research_tool_names())
        for tool in rt:
            if tool.startswith("mcp__research__"):
                assert tool in RESEARCH_TOOLS, (
                    f"research_tool_names() includes {tool} not in RESEARCH_TOOLS policy"
                )

    def test_review_tools_subset_of_research(self) -> None:
        rt = set(review_tool_names())
        full = set(research_tool_names())
        mcp_review = {t for t in rt if t.startswith("mcp__")}
        mcp_full = {t for t in full if t.startswith("mcp__")}
        assert mcp_review <= mcp_full, (
            f"review_tool_names has MCP tools not in research_tool_names: {mcp_review - mcp_full}"
        )
