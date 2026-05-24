"""Tests for pipeline overhaul models: RestartStrategy, PipelineSnapshot, ClassifiedComment."""

import json

import pytest

from inkwell.agent.models import (
    AddAction,
    ArticlePlan,
    ClassifiedComment,
    DropAction,
    MergedDraft,
    PatchAction,
    PipelineSnapshot,
    PreserveAction,
    ResearchCompilation,
    RestartStrategy,
    RewriteAction,
    SectionDraft,
    SectionPlan,
)


class TestRestartStrategyRoundtrip:
    """Discriminated union must serialize and deserialize correctly."""

    def test_roundtrip_all_action_types(self) -> None:
        strategy = RestartStrategy(
            new_plan=None,
            actions=[
                PreserveAction(section="Intro"),
                PatchAction(section="Safety", target_text="old", instruction="reframe"),
                RewriteAction(section="Ethics", reason="wrong angle", new_research_questions=["q1"]),
                AddAction(
                    section_plan=SectionPlan(
                        title="New Section",
                        summary="Added by orchestrator",
                        key_points=["point"],
                    ),
                    insert_after="Intro",
                ),
                DropAction(section="Appendix", reason="no longer relevant"),
            ],
            needs_remerge=True,
            rationale="test",
        )
        data = json.loads(strategy.model_dump_json())
        restored = RestartStrategy.model_validate(data)
        assert len(restored.actions) == 5
        kinds = [a.kind for a in restored.actions]
        assert kinds == ["preserve", "patch", "rewrite", "add", "drop"]

    def test_empty_actions_valid(self) -> None:
        strategy = RestartStrategy(
            actions=[], needs_remerge=False, rationale="no changes"
        )
        assert strategy.model_dump_json()

    def test_discriminator_rejects_unknown_kind(self) -> None:
        raw = {
            "actions": [{"kind": "explode", "section": "X"}],
            "needs_remerge": False,
            "rationale": "bad",
        }
        with pytest.raises(Exception):
            RestartStrategy.model_validate(raw)


class TestPipelineSnapshotClearDownstream:
    """clear_downstream must discard artifacts after the given stage."""

    def make_populated(self) -> PipelineSnapshot:
        return PipelineSnapshot(
            stage="rewrite",
            conversation="source text",
            plan=ArticlePlan(
                title="T",
                thesis="T",
                sections=[],
                research_questions=[],
                source_quotes=[],
                voice_notes="",
                author_direction="",
                target_format="lesswrong",
            ),
            research=ResearchCompilation(findings=[], additional_context=""),
            section_drafts={"Intro": SectionDraft(title="Intro", content="...", word_count=10)},
            merged=MergedDraft(content="merged", changes_made=[]),
            findings=[],
        )

    def test_clear_from_plan_clears_research_and_below(self) -> None:
        snap = self.make_populated()
        snap.clear_downstream("plan")
        assert snap.conversation == "source text"
        assert snap.plan is not None
        assert snap.research is None
        assert snap.section_drafts == {}
        assert snap.merged is None

    def test_clear_from_research_keeps_research_clears_write(self) -> None:
        snap = self.make_populated()
        snap.clear_downstream("research")
        assert snap.research is not None
        assert snap.section_drafts == {}
        assert snap.merged is None

    def test_clear_from_write_keeps_drafts_clears_merge(self) -> None:
        snap = self.make_populated()
        snap.clear_downstream("write")
        assert snap.section_drafts != {}
        assert snap.merged is None

    def test_clear_unknown_stage_is_noop(self) -> None:
        snap = self.make_populated()
        snap.clear_downstream("nonexistent")
        assert snap.plan is not None
        assert snap.research is not None


class TestClassifiedComment:
    """ClassifiedComment validation and defaults."""

    def test_minimal_valid(self) -> None:
        c = ClassifiedComment(
            comment_id="abc",
            content="Fix the intro",
            impact="stage_local",
        )
        assert c.tags == []
        assert c.anchor_text == ""
        assert c.timestamp == ""

    def test_roundtrip_with_tags(self) -> None:
        c = ClassifiedComment(
            comment_id="x",
            content="Wrong thesis",
            impact="plan_breaking",
            tags=["structure", "scope"],
            anchor_text="The main argument",
        )
        restored = ClassifiedComment.model_validate_json(c.model_dump_json())
        assert restored.tags == ["structure", "scope"]
        assert restored.impact == "plan_breaking"
