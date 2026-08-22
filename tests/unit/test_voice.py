"""Tests for voice/style corpus operations."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from inkwell.agent.tools.voice import (
    StyleEntry,
    StyleSample,
    VoiceAnalysis,
    add_format_example,
    add_style_reference,
    analyze_single_source,
    compute_voice_fingerprint,
    extract_author_text,
    invalidate_merged_cache,
    list_format_examples,
    list_style_references,
    load_format_examples,
    load_style_corpus,
    merge_voice_analyses,
    samples_to_analyze,
    speaker_tagged,
    voice_cache_dir,
    voice_cache_key,
    voice_output_server,
    voice_output_tool,
)


@pytest.fixture(autouse=True)
def style_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the corpus — and the voice cache under it — at a per-test directory."""
    corpus = tmp_path / "style"
    corpus.mkdir()
    import inkwell.agent.config as config_mod

    monkeypatch.setattr(config_mod.settings, "style_corpus_path", str(corpus))
    return corpus


class TestLoadStyleCorpus:
    async def test_empty_dir(self, style_dir: Path) -> None:
        assert await load_style_corpus() == []

    async def test_loads_md_files(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("This is my writing style.")
        samples = await load_style_corpus()
        assert [s.label for s in samples] == ["essay.md"]
        assert [s.source_type for s in samples] == ["prose"]

    async def test_loads_txt_files(self, style_dir: Path) -> None:
        (style_dir / "notes.txt").write_text("Some notes here.")
        samples = await load_style_corpus()
        assert [s.label for s in samples] == ["notes.txt"]
        assert [s.source_type for s in samples] == ["prose"]

    async def test_skips_urls_txt(self, style_dir: Path) -> None:
        (style_dir / "urls.txt").write_text("https://example.com\n")
        (style_dir / "real.md").write_text("Real content.")
        labels = [s.label for s in await load_style_corpus()]
        assert "urls.txt" not in labels
        assert "real.md" in labels

    async def test_loads_all_samples(self, style_dir: Path) -> None:
        for i in range(10):
            (style_dir / f"sample_{i:02d}.md").write_text(f"Sample {i}")
        assert len(await load_style_corpus()) == 10

    async def test_preserves_full_content(self, style_dir: Path) -> None:
        (style_dir / "long.md").write_text("x" * 5000)
        samples = await load_style_corpus()
        assert len(samples[0].text) == 5000

    async def test_loads_prescriptive_subdir(self, style_dir: Path) -> None:
        presc_dir = style_dir / "prescriptive"
        presc_dir.mkdir()
        (presc_dir / "rules.md").write_text("Never use em dashes.")
        (style_dir / "essay.md").write_text("My essay.")
        samples = await load_style_corpus()
        assert len(samples) == 2
        by_label = {s.label: s for s in samples}
        assert by_label["essay.md"].source_type == "prose"
        assert by_label["prescriptive/rules.md"].prescriptive

    async def test_nonexistent_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import inkwell.agent.config as config_mod

        monkeypatch.setattr(
            config_mod.settings, "style_corpus_path", "/nonexistent/path"
        )
        assert await load_style_corpus() == []


class TestExtractAuthorText:
    def test_no_tags_returns_unchanged(self) -> None:
        text = "Just a plain writing sample with no speaker tags."
        assert extract_author_text(text) == text

    def test_extracts_user_blocks_only(self) -> None:
        conversation = (
            "<user>\nHere is my prompt.\n</user>\n\n"
            "<claude>\nHere is Claude's response.\n</claude>\n\n"
            "<user>\nMy follow-up.\n</user>"
        )
        result = extract_author_text(conversation)
        assert "Here is my prompt." in result
        assert "My follow-up." in result
        assert "Claude's response" not in result

    def test_multiple_user_blocks_joined(self) -> None:
        conversation = (
            "<user>\nFirst message.\n</user>\n\n"
            "<claude>\nReply.\n</claude>\n\n"
            "<user>\nSecond message.\n</user>"
        )
        result = extract_author_text(conversation)
        assert "First message." in result
        assert "Second message." in result

    def test_empty_user_block_skipped(self) -> None:
        conversation = "<user>\n  \n</user>\n\n<user>\nReal content.\n</user>"
        result = extract_author_text(conversation)
        assert "Real content." in result
        assert result.strip() == "Real content."

    def test_document_with_feedback_conversation_keeps_draft(self) -> None:
        conversation = (
            "--- Source: gdoc ---\n\n"
            "The draft's real thesis is that political will is the bottleneck.\n\n"
            "--- Source: claude-share ---\n\n"
            "<user>\nmake it punchier\n</user>\n\n"
            "<claude>\nA punchier rewrite that is not the author's voice.\n</claude>"
        )
        result = extract_author_text(conversation)
        assert "political will is the bottleneck" in result
        assert "make it punchier" in result
        assert "not the author's voice" not in result


class TestAddStyleReference:
    def test_add_file(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "my_essay.md"
        source.write_text("My writing sample.")
        msg = add_style_reference(str(source))
        assert "Added" in msg
        assert (style_dir / "my_essay.md").exists()
        assert (style_dir / "my_essay.md").read_text() == "My writing sample."

    def test_add_url(self, style_dir: Path) -> None:
        msg = add_style_reference("https://example.com/post")
        assert "Added URL" in msg
        urls_file = style_dir / "urls.txt"
        assert urls_file.exists()
        assert "https://example.com/post" in urls_file.read_text()

    def test_add_duplicate_url(self, style_dir: Path) -> None:
        add_style_reference("https://example.com/post")
        msg = add_style_reference("https://example.com/post")
        assert "Already in corpus" in msg

    def test_add_with_format_delegates(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "good_post.md"
        source.write_text("Example LW post.")
        msg = add_style_reference(str(source), target_format="lesswrong")
        assert "lesswrong" in msg
        assert (style_dir / "formats" / "lesswrong" / "good_post.md").exists()

    def test_add_prescriptive_file(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "rules.md"
        source.write_text("Never use em dashes.")
        msg = add_style_reference(str(source), prescriptive=True)
        assert "prescriptive" in msg
        assert (style_dir / "prescriptive" / "rules.md").exists()

    def test_add_prescriptive_url_rejected(self, style_dir: Path) -> None:
        msg = add_style_reference("https://example.com/guide", prescriptive=True)
        assert "must be local files" in msg


class TestListStyleReferences:
    def test_empty_corpus(self, style_dir: Path) -> None:
        listing = list_style_references()
        assert listing.voice == []
        assert listing.prescriptive == []

    def test_lists_files_and_urls(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("Content")
        (style_dir / "urls.txt").write_text("https://example.com\n")

        kinds = {e.kind for e in list_style_references().voice}
        assert "file" in kinds
        assert "url" in kinds

    def test_entry_types(self, style_dir: Path) -> None:
        (style_dir / "test.md").write_text("hello")
        voice = list_style_references().voice
        assert len(voice) == 1
        assert isinstance(voice[0], StyleEntry)
        assert voice[0].kind == "file"
        assert voice[0].name == "test.md"
        assert voice[0].size > 0

    def test_lists_prescriptive_entries(self, style_dir: Path) -> None:
        presc_dir = style_dir / "prescriptive"
        presc_dir.mkdir()
        (presc_dir / "guide.md").write_text("No em dashes.")
        prescriptive = list_style_references().prescriptive
        assert len(prescriptive) == 1
        assert prescriptive[0].name == "guide.md"


# ---------------------------------------------------------------------------
# Format-specific examples
# ---------------------------------------------------------------------------


class TestLoadFormatExamples:
    async def test_no_format_dir(self, style_dir: Path) -> None:
        assert await load_format_examples("lesswrong") == []

    async def test_loads_format_files(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "memo"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "good-memo.md").write_text("TO: Leadership\nFROM: Author")
        samples = await load_format_examples("memo")
        assert [s.label for s in samples] == ["good-memo.md"]

    async def test_skips_urls_txt(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "blog"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "urls.txt").write_text("https://example.com/post\n")
        (fmt_dir / "example.md").write_text("Blog content.")
        labels = [s.label for s in await load_format_examples("blog")]
        assert "urls.txt" not in labels
        assert "example.md" in labels

    async def test_handles_custom_format_prefix(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "custom"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "example.md").write_text("Custom format content.")
        assert len(await load_format_examples("custom:academic abstract")) == 1


class TestAddFormatExample:
    def test_add_file(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "great-lw-post.md"
        source.write_text("Epistemic status: highly confident")
        msg = add_format_example("lesswrong", str(source))
        assert "lesswrong" in msg
        dest = style_dir / "formats" / "lesswrong" / "great-lw-post.md"
        assert dest.exists()

    def test_add_url(self, style_dir: Path) -> None:
        msg = add_format_example("memo", "https://example.com/memo")
        assert "memo" in msg
        urls_file = style_dir / "formats" / "memo" / "urls.txt"
        assert urls_file.exists()
        assert "https://example.com/memo" in urls_file.read_text()

    def test_add_duplicate_url(self, style_dir: Path) -> None:
        add_format_example("blog", "https://example.com/post")
        msg = add_format_example("blog", "https://example.com/post")
        assert "Already in" in msg

    def test_creates_format_dir(self, style_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "example.md"
        source.write_text("Content")
        add_format_example("dialog", str(source))
        assert (style_dir / "formats" / "dialog").is_dir()


class TestListFormatExamples:
    def test_no_formats_dir(self, style_dir: Path) -> None:
        assert list_format_examples() == {}

    def test_lists_all_formats(self, style_dir: Path) -> None:
        for fmt in ("lesswrong", "memo"):
            fmt_dir = style_dir / "formats" / fmt
            fmt_dir.mkdir(parents=True)
            (fmt_dir / "example.md").write_text(f"{fmt} example")

        result = list_format_examples()
        assert "lesswrong" in result
        assert "memo" in result
        assert len(result["lesswrong"]) == 1

    def test_filters_by_format(self, style_dir: Path) -> None:
        for fmt in ("lesswrong", "memo"):
            fmt_dir = style_dir / "formats" / fmt
            fmt_dir.mkdir(parents=True)
            (fmt_dir / "example.md").write_text(f"{fmt} example")

        result = list_format_examples(target_format="memo")
        assert "memo" in result
        assert "lesswrong" not in result


# ---------------------------------------------------------------------------
# Prescriptive flag parsing
# ---------------------------------------------------------------------------


class TestParsePrescriptiveFlag:
    def test_no_frontmatter(self) -> None:
        one = VoiceAnalysis.from_cached("s", "Just a regular analysis.")
        assert one.analysis == "Just a regular analysis."
        assert not one.prescriptive

    def test_prescriptive_true(self) -> None:
        text = "---\nprescriptive: true\n---\nBrief summary."
        one = VoiceAnalysis.from_cached("s", text)
        assert one.analysis == "Brief summary."
        assert one.prescriptive

    def test_prescriptive_false(self) -> None:
        text = "---\nprescriptive: false\n---\nFull voice analysis."
        one = VoiceAnalysis.from_cached("s", text)
        assert one.analysis == "Full voice analysis."
        assert not one.prescriptive

    def test_case_insensitive(self) -> None:
        text = "---\nPrescriptive: True\n---\nContent."
        assert VoiceAnalysis.from_cached("s", text).prescriptive

    def test_missing_closing_fence(self) -> None:
        text = "---\nprescriptive: true\nNo closing fence."
        one = VoiceAnalysis.from_cached("s", text)
        assert one.analysis == text
        assert not one.prescriptive

    def test_empty_body_returns_original(self) -> None:
        text = "---\nprescriptive: true\n---\n"
        one = VoiceAnalysis.from_cached("s", text)
        assert one.analysis == text
        assert one.prescriptive

    def test_keeps_the_label_it_was_given(self) -> None:
        assert VoiceAnalysis.from_cached("my-label", "text").label == "my-label"


# ---------------------------------------------------------------------------
# Per-source voice analysis caching
# ---------------------------------------------------------------------------


class TestVoiceCaching:
    def test_cache_key_deterministic(self) -> None:
        text = "Same input every time."
        assert voice_cache_key(text) == voice_cache_key(text)

    def test_cache_key_differs_for_different_input(self) -> None:
        assert voice_cache_key("text A") != voice_cache_key("text B")

    def test_cache_dir_created(self, style_dir: Path) -> None:
        cache = voice_cache_dir()
        assert cache.exists()
        assert cache == style_dir / ".cache" / "voice"

    @pytest.mark.asyncio
    async def test_analyze_single_source_caches(self, style_dir: Path) -> None:
        text = "The author writes with dry humor and short sentences."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text(
            "---\nprescriptive: false\n---\nCached analysis: dry humor, short sentences."
        )

        one = await analyze_single_source(StyleSample(label="test-sample", text=text))
        assert "dry humor" in one.analysis
        assert not one.prescriptive

    @pytest.mark.asyncio
    async def test_the_analyst_hands_its_guide_back_rather_than_writing_it(
        self, style_dir: Path
    ) -> None:
        """It ran with no write tool at all, which is why this has to work: an
        analyst told to write a file was refused one — a whole-file write is an
        approval, and an unattended run has nobody to ask — and every caller
        drops an empty analysis, so the piece was written with no voice guide
        and nothing said so."""
        target = voice_cache_dir() / "handed.md"

        await voice_output_tool(target).handler({"guide": "Dry humor, short."})

        assert target.read_text() == "Dry humor, short."
        assert voice_output_server(target, "voice_analysis").tool_names == [
            "mcp__voice_analysis__submit_voice_analysis"
        ]

    @pytest.mark.asyncio
    async def test_an_empty_guide_is_refused_rather_than_kept(
        self, style_dir: Path
    ) -> None:
        """Kept, it would read downstream as a sample with no voice in it."""
        target = voice_cache_dir() / "empty.md"

        answered = await voice_output_tool(target).handler({"guide": "   "})

        assert answered.get("is_error")
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_analyze_single_source_caches_prescriptive(
        self, style_dir: Path
    ) -> None:
        text = "Never use em dashes. Always use active voice."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text("---\nprescriptive: true\n---\nBrief summary of rules.")

        one = await analyze_single_source(StyleSample(label="rules", text=text))
        assert one.prescriptive
        assert "summary" in one.analysis

    @pytest.mark.asyncio
    async def test_analyze_single_source_old_cache_no_frontmatter(
        self, style_dir: Path
    ) -> None:
        text = "Legacy cached analysis without frontmatter."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text("Old analysis without YAML frontmatter.")

        one = await analyze_single_source(StyleSample(label="legacy", text=text))
        assert one.analysis == "Old analysis without YAML frontmatter."
        assert not one.prescriptive

    @pytest.mark.asyncio
    async def test_analyze_single_source_calls_query_on_miss(
        self, style_dir: Path
    ) -> None:
        text = "New text to analyze."
        key = voice_cache_key(text)
        cache_path = voice_cache_dir() / f"{key}.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            cache_path.write_text(
                "---\nprescriptive: false\n---\nFresh analysis result."
            )
            return type("C", (), {"text": ""})()

        with patch(
            "inkwell.agent.tools.voice.query",
            new_callable=AsyncMock,
            side_effect=fake_query,
        ):
            one = await analyze_single_source(
                StyleSample(label="new-sample", text=text)
            )

        assert one.analysis == "Fresh analysis result."
        assert not one.prescriptive

    @pytest.mark.asyncio
    async def test_analyze_single_source_label_identity(self, style_dir: Path) -> None:
        """Labels travel with results — the core identity bug fix."""
        text = "Some writing sample."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text("---\nprescriptive: false\n---\nAnalysis.")

        one = await analyze_single_source(StyleSample(label="my-label", text=text))
        assert one.label == "my-label"
        assert "Analysis" in one.analysis


class TestMergeVoiceAnalyses:
    @pytest.mark.asyncio
    async def test_merge_combines_analyses(self, tmp_path: Path) -> None:
        output_path = tmp_path / "voice.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            output_path.write_text("Merged guide content.")
            return type("C", (), {"text": ""})()

        with patch(
            "inkwell.agent.tools.voice.query",
            new_callable=AsyncMock,
            side_effect=fake_query,
        ):
            result = await merge_voice_analyses(
                [
                    VoiceAnalysis(label="source-1", analysis="Analysis 1"),
                    VoiceAnalysis(label="source-2", analysis="Analysis 2"),
                ],
                output_path,
            )
        assert result == "Merged guide content."

    @pytest.mark.asyncio
    async def test_merge_includes_format_examples(self, tmp_path: Path) -> None:
        output_path = tmp_path / "voice.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            output_path.write_text("Guide with format awareness.")
            return type("C", (), {"text": ""})()

        with patch(
            "inkwell.agent.tools.voice.query",
            new_callable=AsyncMock,
            side_effect=fake_query,
        ) as mock_query:
            await merge_voice_analyses(
                [VoiceAnalysis(label="source-1", analysis="Analysis 1")],
                output_path,
                format_examples=[
                    VoiceAnalysis(
                        label="good-post.md", analysis="Example LW post content"
                    )
                ],
            )
        prompt = mock_query.call_args[0][0]
        assert "merge_input.md" in prompt
        merge_input = Path(voice_cache_dir() / "merge_input.md").read_text(
            encoding="utf-8"
        )
        assert "Format Reference Analyses" in merge_input
        assert "good-post.md" in merge_input


class TestVoiceFingerprint:
    async def test_deterministic(self, style_dir: Path) -> None:
        fp1 = await compute_voice_fingerprint("conv", ["sample1"], "blog")
        fp2 = await compute_voice_fingerprint("conv", ["sample1"], "blog")
        assert fp1 == fp2

    async def test_changes_with_conversation(self, style_dir: Path) -> None:
        fp1 = await compute_voice_fingerprint("conv A", ["s"], "blog")
        fp2 = await compute_voice_fingerprint("conv B", ["s"], "blog")
        assert fp1 != fp2

    async def test_changes_with_corpus(self, style_dir: Path) -> None:
        fp1 = await compute_voice_fingerprint("conv", ["old sample"], "blog")
        fp2 = await compute_voice_fingerprint("conv", ["new sample"], "blog")
        assert fp1 != fp2

    async def test_changes_with_format(self, style_dir: Path) -> None:
        fp1 = await compute_voice_fingerprint("conv", ["s"], "blog")
        fp2 = await compute_voice_fingerprint("conv", ["s"], "lesswrong")
        assert fp1 != fp2

    async def test_changes_with_added_corpus(self, style_dir: Path) -> None:
        fp1 = await compute_voice_fingerprint("conv", ["s1"], "blog")
        fp2 = await compute_voice_fingerprint("conv", ["s1", "s2"], "blog")
        assert fp1 != fp2


class TestInvalidateMergedCache:
    def test_removes_merged_guide(self, style_dir: Path) -> None:
        cache = voice_cache_dir()
        merged = cache / "merged_guide.md"
        merged.write_text("old guide")
        invalidate_merged_cache()
        assert not merged.exists()

    def test_noop_when_no_cache(self, style_dir: Path) -> None:
        invalidate_merged_cache()


SIGNATURE_PROSE = (
    "Moreover, the model — trained on nearly everything — is expensive.\n\n"
    "Moreover, the cache — rebuilt every night — is already stale.\n\n"
    "Moreover, the budget — approved last quarter — is gone."
)
"""Prose in an author's own signature: em-dash-heavy, copular, and opening
every paragraph the same way. The textbook rows fire on all three."""


class TestVoiceOutranksFormatChecks:
    """A row firing against the author's own habits does not license a rewrite."""

    async def test_rows_fire_on_a_passage_the_voice_profile_sanctions(self) -> None:
        from inkwell.agent.format_checks import run_format_checks
        from inkwell.agent.segmenter import reader
        from inkwell.agent.stages import format_checks_for
        from tests.unit.test_format_checks import HELD, StubJudge

        profile = VoiceAnalysis(
            label="author",
            analysis=(
                "The author writes long em-dash-heavy sentences and opens "
                "paragraph after paragraph with 'Moreover'. This is a "
                "signature, not a slip — keep it."
            ),
        )
        mechanical = [c for c in format_checks_for("textbook") if c.mechanical]
        report = await run_format_checks(
            SIGNATURE_PROSE,
            mechanical,
            format_key="textbook",
            draft_path=Path("draft.md"),
            judge=StubJudge(HELD),
            reader=reader(),
        )

        fired = {row.name for row in report.fired}
        assert {"em-dash density", "paragraph openings"} <= fired
        assert "signature" in profile.analysis

        rendered = report.render()
        assert "advisory" in rendered
        assert "outrank" in rendered
        assert "the author wins" in rendered

    async def test_the_report_defers_to_the_voice_where_it_is_read(
        self, tmp_path: Path
    ) -> None:
        """The rewrite stage gets the voice profile and the rows side by side."""
        from inkwell.agent.content import ContentManifest
        from inkwell.agent.notes import PipelineNotes
        from inkwell.agent.pipeline import add_format_check_report, add_voice_refs
        from tests.unit.test_format_checks import HELD, StubJudge

        voice_file = tmp_path / "voice_profile.md"
        voice_file.write_text("The author's em dashes are the point.", encoding="utf-8")
        draft_path = tmp_path / "draft.md"
        draft_path.write_text(SIGNATURE_PROSE, encoding="utf-8")

        manifest = ContentManifest()
        add_voice_refs(manifest, [str(voice_file)])
        await add_format_check_report(
            manifest,
            PipelineNotes(tmp_path),
            SIGNATURE_PROSE,
            draft_path,
            target_format="twitter",
            judge=StubJudge(HELD),
        )

        labels = [ref.label for ref in manifest.refs]
        assert "Format checks" in labels
        assert len(labels) > 1, "the voice profile is listed beside the rows"
        rows = next(ref for ref in manifest.refs if ref.label == "Format checks")
        assert "voice outranks every row" in rows.instruction
        assert "keep the" in rows.instruction


class TestTheSampleTypeIsReadOffTheSample:
    """What the analyst is told about a sample follows the text, not the door
    it came in through.

    The session's own source used to be declared a conversation whatever it
    was, so a `revise` run over a published chapter handed the analyst a
    transcript's reading instructions — analyse the <user> blocks, separate the
    author's instruction voice from their prose voice — for a document that has
    no turns at all.
    """

    def test_a_share_link_transcript_is_a_conversation(self) -> None:
        text = "<user>\nmake it sharper\n</user>\n<claude>\nsure\n</claude>"

        assert speaker_tagged(text)
        assert samples_to_analyze(text, [])[0].source_type == "conversation"

    def test_a_published_chapter_is_prose(self) -> None:
        text = "**Deception is hard to measure.** Chapter 2 opens on the gap."

        assert not speaker_tagged(text)
        assert samples_to_analyze(text, [])[0].source_type == "prose"

    def test_the_corpus_is_typed_the_same_way(self) -> None:
        sample = StyleSample(label="essay", text="Plain prose.", source_type="ignored")

        assert samples_to_analyze("draft", [sample])[1].source_type == "prose"
