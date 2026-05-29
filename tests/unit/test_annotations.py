"""Tests for inline review annotation logic."""

from typing import Literal

from inkwell.agent.models import ReviewFinding
from inkwell.agent.pipeline import annotate_draft_with_findings


def make_finding(
    severity: Literal["critical", "suggestion", "praise"] = "critical",
    reviewer: str = "narrative",
    text_excerpt: str = "",
    issue: str = "test issue",
    suggestion: str = "test fix",
    location: str = "Section 1",
) -> ReviewFinding:
    return ReviewFinding(
        reviewer=reviewer,
        severity=severity,
        location=location,
        issue=issue,
        suggestion=suggestion,
        text_excerpt=text_excerpt,
    )


def test_anchors_finding_at_excerpt() -> None:
    draft = "The policy was enacted in 2020. It changed everything."
    findings = [
        make_finding(
            text_excerpt="enacted in 2020",
            issue="Wrong year",
            suggestion="Should be 2021",
        ),
    ]
    result = annotate_draft_with_findings(draft, findings)
    assert "enacted in 2020" in result
    assert "[FINDING:critical:narrative] Wrong year" in result
    assert "→ Should be 2021" in result


def test_unanchored_findings_in_fallback_list() -> None:
    draft = "Some text about policy."
    findings = [
        make_finding(
            text_excerpt="not in the draft at all",
            issue="Misquote",
            suggestion="Fix quote",
        ),
    ]
    result = annotate_draft_with_findings(draft, findings)
    assert "## Additional Review Findings" in result
    assert "Misquote" in result


def test_findings_without_excerpt_go_to_fallback() -> None:
    draft = "A paragraph of text."
    findings = [
        make_finding(
            text_excerpt="",
            issue="Weak opening",
            suggestion="Rewrite",
        ),
    ]
    result = annotate_draft_with_findings(draft, findings)
    assert "## Additional Review Findings" in result
    assert "Weak opening" in result


def test_praise_findings_excluded() -> None:
    draft = "Excellent prose here."
    findings = [
        make_finding(
            severity="praise",
            text_excerpt="Excellent prose",
            issue="Great writing",
        ),
    ]
    result = annotate_draft_with_findings(draft, findings)
    assert "[FINDING:" not in result
    assert "## Additional" not in result


def test_longer_excerpt_takes_priority_over_substring() -> None:
    """When a short excerpt is a substring of a longer one, the longer one anchors inline
    and the shorter one falls to the unanchored list (since its text was already annotated)."""
    draft = "The framework provides clarity and structure. Other text."
    findings = [
        make_finding(
            text_excerpt="The framework provides clarity",
            issue="Short match",
            suggestion="Fix short",
        ),
        make_finding(
            text_excerpt="The framework provides clarity and structure",
            issue="Long match",
            suggestion="Fix long",
        ),
    ]
    result = annotate_draft_with_findings(draft, findings)
    assert "Long match" in result
    assert "Short match" in result


def test_only_first_occurrence_annotated() -> None:
    draft = "Error here. Some text. Error here."
    findings = [
        make_finding(text_excerpt="Error here", issue="Fix this"),
    ]
    result = annotate_draft_with_findings(draft, findings)
    count = result.count("[FINDING:")
    assert count == 1
