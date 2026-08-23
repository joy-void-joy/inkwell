"""Batch stage outputs preserve the records accepted by their singular tools."""

from pathlib import Path
from typing import Literal

import pytest

from lup.mcp import ToolError

from inkwell.agent.tools.stage_outputs import (
    DispositionCollector,
    RecordDispositionInput,
    RecordDispositionsInput,
    RecordFindingInput,
    RecordFindingsInput,
    RecordReviewFindingInput,
    RecordReviewFindingsInput,
    ResearchCollector,
    ReviewCollector,
    make_disposition_tools,
    make_research_output_tools,
    make_review_output_tools,
)


def tool(tools: list, name: str):
    """One named tool from a stage-local server."""
    return next(one for one in tools if one.name == name)


def research(
    question: str,
    *,
    origin: Literal[
        "source_document", "external", "mixed", "author_unverified"
    ] = "external",
) -> RecordFindingInput:
    """One source-free finding sufficient to exercise collector behavior."""
    return RecordFindingInput(
        question=question,
        answer=f"Answer to {question}",
        origin=origin,
        confidence=0.8,
        sources=[],
    )


async def test_research_batch_preserves_order_and_notifies_once(tmp_path: Path) -> None:
    notifications = 0

    async def notified() -> None:
        nonlocal notifications
        notifications += 1

    collector = ResearchCollector(tmp_path / "research.json", on_save=notified)
    record = tool(make_research_output_tools(collector), "record_findings")

    result = await record.call_handler(
        RecordFindingsInput(findings=[research("first"), research("second")])
    )

    assert [one.question for one in collector.research.findings] == ["first", "second"]
    assert [one.question for one in result.findings] == ["first", "second"]
    assert notifications == 1


async def test_research_batch_saves_nothing_when_one_finding_is_invalid(
    tmp_path: Path,
) -> None:
    collector = ResearchCollector(tmp_path / "research.json")
    record = tool(make_research_output_tools(collector), "record_findings")

    with pytest.raises(ToolError, match="source_document"):
        await record.call_handler(
            RecordFindingsInput(
                findings=[
                    research("valid"),
                    research("invalid", origin="source_document"),
                ]
            )
        )

    assert collector.research.findings == []
    assert not collector.output_path.exists()


def review(issue: str) -> RecordReviewFindingInput:
    """One complete reviewer finding."""
    return RecordReviewFindingInput(
        severity="suggestion",
        location="opening",
        issue=issue,
        suggestion=f"Address {issue}",
        text_excerpt="The opening.",
    )


async def test_review_batch_assigns_reviewer_to_every_finding(tmp_path: Path) -> None:
    collector = ReviewCollector(tmp_path / "review.json", reviewer="narrative")
    record = tool(make_review_output_tools(collector), "record_findings")

    await record.call_handler(
        RecordReviewFindingsInput(findings=[review("first"), review("second")])
    )

    assert [one.issue for one in collector.review.findings] == ["first", "second"]
    assert {one.reviewer for one in collector.review.findings} == {"narrative"}


async def test_disposition_batch_preserves_every_tag_in_order(tmp_path: Path) -> None:
    collector = DispositionCollector(tmp_path / "dispositions.json")
    record = tool(make_disposition_tools(collector), "record_dispositions")
    inputs = [
        RecordDispositionInput(tag="F01", action="applied", reason="Reframed it."),
        RecordDispositionInput(
            tag="F02", action="rejected", reason="Source disproves it."
        ),
    ]

    await record.call_handler(RecordDispositionsInput(dispositions=inputs))

    assert [one.tag for one in collector.record.dispositions] == ["F01", "F02"]
    assert collector.output_path.exists()
