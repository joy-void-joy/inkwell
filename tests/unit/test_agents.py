"""Tests for pipeline stage prompts and tool list consistency."""

from inkwell.agent.stages import (
    FACT_CHECKER_PROMPT,
    NARRATIVE_REVIEWER_PROMPT,
    PLANNER_SYSTEM,
    MERGE_PROMPT,
    RESEARCHER_PROMPT,
    REWRITER_SYSTEM,
    SECTION_WRITER_PROMPT,
    STYLE_REVIEWER_PROMPT,
)
from inkwell.agent.pipeline import build_research_tools
from inkwell.agent.tool_policy import (
    RESEARCH_TOOLS,
    research_name,
    research_tool_names,
    review_tool_names,
)
from inkwell.agent.tools.research.corpus import CORPUS_TOOLS


ALL_PROMPTS = {
    "planner": PLANNER_SYSTEM,
    "researcher": RESEARCHER_PROMPT,
    "section_writer": SECTION_WRITER_PROMPT,
    "merge": MERGE_PROMPT,
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
            "merge",
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

    def test_every_mounted_research_tool_is_callable(self) -> None:
        """A tool the server mounts but the allowlist omits is registered and
        uncallable, which reads to a stage exactly like a tool that is broken."""
        allowed = research_tool_names()
        for tool in build_research_tools():
            assert research_name(tool) in allowed, (
                f"{tool.name} is mounted on the research server but not allowed"
            )

    def test_the_corpus_leads_the_research_surface(self) -> None:
        """What is already gathered comes before what has to be fetched."""
        mounted = [tool.name for tool in build_research_tools()]
        assert mounted[: len(CORPUS_TOOLS)] == [tool.name for tool in CORPUS_TOOLS]

    def test_corpus_retrieval_reaches_the_research_and_review_stages(self) -> None:
        assert "mcp__research__search_corpus" in research_tool_names()
        assert "mcp__research__search_corpus" in review_tool_names()

    def test_review_tools_subset_of_research(self) -> None:
        rt = set(review_tool_names())
        full = set(research_tool_names())
        mcp_review = {t for t in rt if t.startswith("mcp__")}
        mcp_full = {t for t in full if t.startswith("mcp__")}
        assert mcp_review <= mcp_full, (
            f"review_tool_names has MCP tools not in research_tool_names: {mcp_review - mcp_full}"
        )
