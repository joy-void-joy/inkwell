"""What a single writer has done so far, read off the draft it is writing.

Section statuses were only ever moved by the parallel section writers, so a
run drafting a whole piece in one pass showed a counter that could not leave
zero for the length of the stage, beside a heartbeat counting seconds. The
draft on disk is re-read every eight seconds to sync the Doc; these tests pin
that the same read now says how far the piece has got.
"""

from pathlib import Path

import pytest

import inkwell.agent.pipeline as pipeline
from inkwell.agent.session import WritingSessionState


PLANNED = [
    "Cyber Risk With an Incident Record",
    "What Attackers Actually Buy From a Model",
    "Measured Autonomy, Doubling in Months",
]

HEADED = """## Cyber Risk With an Incident Record

Operations run by human attackers using AI models are dated and attributed.

## What attackers actually buy from a model

Mostly the labour before engagement, not judgment inside a network.
"""

CONTINUOUS = """## 2.3.2 Cyber Risk {: #02}

**What does the record of AI-enabled cyber attacks now show?** Operations run
by human attackers using AI models are dated and attributed rather than
projected.

**What attackers actually buy from a model** is mostly the labour before
engagement, not judgment inside a network.
"""


def test_a_heading_marks_its_planned_section_as_landed() -> None:
    assert pipeline.sections_landed(HEADED, PLANNED) == [
        "Cyber Risk With an Incident Record",
        "What Attackers Actually Buy From a Model",
    ]


def test_a_section_announced_in_bold_counts_like_one_given_a_heading() -> None:
    assert pipeline.sections_landed(CONTINUOUS, PLANNED) == [
        "What Attackers Actually Buy From a Model"
    ]


def test_a_hash_inside_a_fence_is_not_a_heading() -> None:
    fenced = "```\n## Cyber Risk With an Incident Record\n```\n"

    assert pipeline.sections_landed(fenced, PLANNED) == []


def test_a_part_that_announces_nothing_leaves_every_section_where_it_was() -> None:
    prose = "The scarce input in a cyber operation was always operator time.\n"

    assert pipeline.sections_landed(prose, PLANNED) == []


async def test_the_length_reaches_a_watcher_even_when_no_section_is_named(
    tmp_path: Path,
) -> None:
    """The case the badge was stuck on: continuous prose, nothing to count."""
    draft = tmp_path / "output.md"
    draft.write_text("one two three four five", encoding="utf-8")
    state = WritingSessionState()
    seen: list[str] = []

    async def watched(content: str) -> None:
        seen.append(content)
        state.record_draft(len(content.split()), target=1534)

    syncer = pipeline.DraftSyncer("", "", draft, session_state=state, on_draft=watched)
    await syncer.sync()

    assert seen == ["one two three four five"]
    assert state.drafted_words == 5
    assert state.target_words == 1534


async def test_a_draft_that_did_not_change_says_nothing_again(
    tmp_path: Path,
) -> None:
    draft = tmp_path / "output.md"
    draft.write_text("one two", encoding="utf-8")
    calls: list[str] = []

    async def watched(content: str) -> None:
        calls.append(content)

    syncer = pipeline.DraftSyncer("", "", draft, on_draft=watched)
    await syncer.sync()
    await syncer.sync()

    assert calls == ["one two"]


@pytest.mark.parametrize(
    ("planned", "heading"),
    [
        (
            "Discovery Got Cheap; Remediation Did Not",
            "## Discovery got cheap — remediation did not",
        ),
        ("The Overhang and the Bottleneck", "## the overhang and the bottleneck!"),
    ],
)
def test_punctuation_and_casing_do_not_hide_a_section_that_landed(
    planned: str, heading: str
) -> None:
    assert pipeline.sections_landed(f"{heading}\n\nProse.\n", [planned]) == [planned]
