"""Reflection tool — forced self-assessment before output finalization.

This is a TEMPLATE. Customize the input model and reviewer prompt
for your domain.

Pattern: A tool the agent calls to record its self-assessment before
producing final output. A :class:`~lup.lib.reflect.ReflectionGate`
hook enforces this — StructuredOutput (or sleep) is denied until
the agent has called ``review``.

Optionally runs a reviewer sub-agent (independent ClaudeSDKClient)
that critiques the main agent's reasoning with sandboxed file access
to past outputs and web search.

Usage in core.py:
    1. Call ``create_reflect_tools(session_dir=..., outputs_dir=...)``
    2. Register the tools as an MCP server
    3. Wire ``create_reflection_gate(gate=kit["gate"], ...)`` into hooks

Tool naming convention:
    After registration: ``mcp__{server_name}__review``
    Example: ``mcp__notes__review``
"""

import json
import logging
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, Field

from lup.client import query
from lup.mcp import LupMcpTool, lup_tool
from lup.reflect import ReflectionGate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reviewer system prompt (customize for your domain)
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM_PROMPT = """\
You review the writing agent's work before finalization. Your job is to catch \
issues that the parallel reviewers might miss — meta-level concerns about the \
overall pipeline execution.

## What to flag

**Pipeline completeness:**
- Were all planned sections actually written?
- Did the researcher address all research questions?
- Were reviewer findings actually incorporated in the rewrite?
- Are there Google Doc comments from the author that went unaddressed?

**Voice consistency:**
- Does the final draft sound like the author (based on the source conversation)?
- Are there sections where the voice shifts noticeably?
- Did the style corpus get used effectively?

**Research quality:**
- Are claims well-sourced or are there unsupported assertions?
- Were prediction market signals considered where relevant?
- Is the research depth proportional to the claim's importance?

**Structural coherence:**
- Does the piece build a clear argument or just list observations?
- Is the conclusion earned by the preceding sections?
- Would a reader who knows nothing about the topic follow this?

If the work is solid, say so briefly. Don't fabricate concerns.

## Historical data

Past outputs at: {outputs_dir}/

## Format

Brief structured critique. Be specific — cite the exact section or claim.
"""


# ---------------------------------------------------------------------------
# Input model (customize for your domain)
# ---------------------------------------------------------------------------


class ReflectInput(BaseModel):
    """Writing-specific reflection input."""

    assessment: str = Field(
        description=(
            "Assessment of the writing session: how well does the draft "
            "capture the source material's ideas? Is the voice consistent? "
            "Are all planned sections complete?"
        ),
    )
    confidence: float = Field(
        description="Confidence in the draft quality (0.0-1.0).",
    )
    sections_status: str = Field(
        description="Status of each planned section: written, partial, or missing.",
    )
    voice_assessment: str = Field(
        description=(
            "How well does the draft match the author's voice? "
            "Where does it drift toward generic AI prose?"
        ),
    )
    research_gaps: str | None = Field(
        default=None,
        description="Claims that still lack adequate sourcing.",
    )
    key_uncertainties: str | None = Field(
        default=None,
        description="What you're most uncertain about.",
    )
    tool_audit: str = Field(
        description=(
            "Which tools provided useful information, which returned "
            "empty results, and which had actual failures."
        ),
    )
    process_reflection: str = Field(
        description=(
            "How did the system feel to use — not what you did, but how the "
            "scaffolding supported you. What felt rigid or lacking, what felt "
            "smooth? Where did you hit friction?"
        ),
    )
    skip_reviewer: bool = Field(
        default=False,
        description="Skip the reviewer sub-agent.",
    )


class ReviewOutput(BaseModel):
    """Output from the reflection tool."""

    status: str = Field(description="Review status (e.g. 'reviewed')")
    assessment_saved: str = Field(description="Path where assessment was saved")
    process_reflection: str = Field(description="Agent's process reflection")
    tool_audit: str = Field(description="Agent's tool usage audit")
    reviewer_critique: str = Field(description="Reviewer critique or skip reason")


# ---------------------------------------------------------------------------
# Reviewer sub-agent
# ---------------------------------------------------------------------------


async def run_reviewer(
    validated: ReflectInput,
    outputs_dir: Path | None,
    *,
    model: str = "claude-sonnet-4-6",
) -> str | None:
    """Run the reviewer sub-agent and return its critique text.

    Args:
        validated: The reflection input from the main agent.
        outputs_dir: Path to past outputs for historical calibration.
        model: Model to use for the reviewer (default: claude-sonnet-4-6).
    """
    prompt_sections = [
        "## Agent Assessment\n\n" + validated.assessment,
        f"## Confidence: {validated.confidence:.0%}",
    ]
    if validated.key_uncertainties:
        prompt_sections.append("## Key Uncertainties\n\n" + validated.key_uncertainties)

    reviewer_prompt = "\n\n".join(prompt_sections)

    collector = await query(
        reviewer_prompt,
        prefix="  ↳ [reviewer] ",
        model=model,
        system_prompt=REVIEWER_SYSTEM_PROMPT.format(
            outputs_dir=outputs_dir or "N/A",
        ),
        max_thinking_tokens=8000,
        permission_mode="bypassPermissions",
        tools=["Read", "Glob", "Grep", "WebFetch"],
        max_turns=5,
    )

    return collector.text


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


class ReflectToolKit(TypedDict):
    """Return type for :func:`create_reflect_tools`."""

    tools: list[LupMcpTool]
    gate: ReflectionGate


def create_reflect_tools(
    *,
    session_dir: Path,
    outputs_dir: Path | None = None,
    gate: ReflectionGate | None = None,
    reviewer_model: str = "claude-sonnet-4-6",
) -> ReflectToolKit:
    """Create the reflection tool(s) and their gate state.

    Returns both the tools (for MCP server registration) and the
    gate (for wiring into :func:`~lup.lib.reflect.create_reflection_gate`).

    Args:
        session_dir: Where to save the review output (JSON).
        outputs_dir: Path to past outputs for the reviewer to Read.
            If None, the reviewer won't have historical data access.
        gate: External gate instance to use. Creates a new one if None.
    """
    gate = gate or ReflectionGate()

    @lup_tool(
        "Structured self-review before finalizing output. Call this tool "
        "after completing your research and analysis but before producing "
        "your final structured output. Runs an independent reviewer that "
        "critiques your reasoning, checks for gaps, and flags calibration "
        "issues. Use the reviewer's feedback to adjust your output. "
        "You must call this at least once per session."
    )
    async def review(validated: ReflectInput) -> ReviewOutput:
        # Save the review input
        session_dir.mkdir(parents=True, exist_ok=True)
        review_path = session_dir / "review.json"
        review_path.write_text(
            json.dumps(validated.model_dump(), indent=2), encoding="utf-8"
        )

        gate.mark_reflected()

        critique: str | None = None
        if not validated.skip_reviewer:
            try:
                critique = await run_reviewer(
                    validated, outputs_dir, model=reviewer_model
                )
            except (RuntimeError, OSError, TimeoutError, ValueError):
                logger.exception("Reviewer sub-agent failed")
                critique = "(reviewer error — see logs)"

        return ReviewOutput(
            status="reviewed",
            assessment_saved=str(review_path),
            process_reflection=validated.process_reflection,
            tool_audit=validated.tool_audit,
            reviewer_critique=critique or "(skipped or failed)",
        )

    return ReflectToolKit(tools=[review], gate=gate)
