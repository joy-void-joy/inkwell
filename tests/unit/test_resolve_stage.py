"""The resolver writes through its stage mount, then persists from the host."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import inkwell.agent.pipeline as pipeline
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineRunner, StageCompute
from inkwell.agent.tools.source_consult import SourceDocument


async def test_resolver_promotes_complete_stage_output(
    tmp_path: Path, monkeypatch
) -> None:
    notes = PipelineNotes(tmp_path / "pipeline_notes")
    runner = PipelineRunner(sources=["source.md"], notes=notes)
    runner.state.pending_questions = ["Which source settles this?"]
    stage_output = notes.work_dir / "resolve" / "output.md"
    stage_output.parent.mkdir(parents=True)
    prompts: list[str] = []

    source = SourceDocument(label="source", path="/source.md", kind="text")
    monkeypatch.setattr(pipeline, "load_source_registry", lambda _path: [source])
    monkeypatch.setattr(pipeline, "render_source_lines", lambda _notes: "")

    @asynccontextmanager
    async def compute(_label: str) -> AsyncIterator[StageCompute]:
        yield StageCompute(
            servers={}, tool_names=[], output_path=stage_output, sandbox=None
        )

    async def resolve(prompt: str, **_kwargs: object) -> None:
        prompts.append(prompt)
        stage_output.write_text("- Q: One\n  A: Settled.\n", encoding="utf-8")

    runner.stage_compute = compute  # type: ignore[method-assign]
    monkeypatch.setattr(pipeline, "query", resolve)

    await runner.stage_resolve()

    durable = notes.artifacts_dir / "resolutions.md"
    assert durable.read_text(encoding="utf-8") == "- Q: One\n  A: Settled.\n"
    assert str(stage_output) in prompts[0]
    assert str(durable) not in prompts[0]


async def test_resolver_skips_material_without_factual_authority(
    tmp_path: Path, monkeypatch
) -> None:
    runner = PipelineRunner(sources=["standing.md"], notes=PipelineNotes(tmp_path))
    runner.state.pending_questions = ["Should this old claim survive?"]
    material = SourceDocument(
        label="standing", path="/standing.md", kind="text", factual_authority=False
    )
    monkeypatch.setattr(pipeline, "load_source_registry", lambda _path: [material])
    monkeypatch.setattr(pipeline, "query", lambda *_args, **_kwargs: pytest.fail())

    await runner.stage_resolve()
