"""Runtime style references feed voice analysis, never the content blob.

A style reference is material whose *voice* the article emulates, not material
whose *content* it incorporates. The extract stage pulls each style ref's prose
into a separate channel (``style_ref_samples``) that the voice stage merges with
the persistent corpus. These tests pin the extraction's two load-bearing
properties: failed or empty extractions are dropped, and the surviving samples
stay index-aligned with their source labels (the voice stage zips the two).
"""

from pathlib import Path

import pytest

import inkwell.agent.pipeline as pipeline_module
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.pipeline import PipelineRunner


def make_runner(tmp_path: Path, style_refs: list[str]) -> PipelineRunner:
    runner = PipelineRunner(sources=["dummy"], notes=PipelineNotes(tmp_path / "notes"))
    runner.style_refs = style_refs
    return runner


@pytest.fixture
def patch_extract(monkeypatch: pytest.MonkeyPatch):
    def install(behavior):
        async def fake_extract(url: str, _existing_doc_id: str | None) -> str:
            return behavior(url)

        monkeypatch.setattr(pipeline_module, "extract_single_source", fake_extract)

    return install


async def test_empty_style_refs_returns_empty(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [])
    assert await runner.extract_style_samples() == ([], [])


async def test_failed_extraction_dropped_and_alignment_preserved(
    tmp_path: Path, patch_extract
) -> None:
    def behavior(url: str) -> str:
        if url == "bad":
            raise RuntimeError("extractor exploded")
        return f"prose:{url}"

    patch_extract(behavior)
    runner = make_runner(tmp_path, ["a", "bad", "c"])

    samples, labels = await runner.extract_style_samples()

    assert samples == ["prose:a", "prose:c"]
    assert labels == ["a", "c"]


async def test_blank_extraction_dropped(tmp_path: Path, patch_extract) -> None:
    patch_extract(lambda url: "" if url == "empty" else f"prose:{url}")
    runner = make_runner(tmp_path, ["empty", "real"])

    samples, labels = await runner.extract_style_samples()

    assert samples == ["prose:real"]
    assert labels == ["real"]
