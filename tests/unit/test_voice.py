"""Tests for voice/style corpus operations."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from inkwell.agent.tools.voice import (
    StyleEntry,
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
    parse_prescriptive_flag,
    voice_cache_dir,
    voice_cache_key,
)


@pytest.fixture
def style_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary style corpus directory."""
    corpus = tmp_path / "style"
    corpus.mkdir()
    monkeypatch.setattr(
        "inkwell.agent.config.settings",
        type("S", (), {"style_corpus_path": str(corpus)})(),
    )
    return corpus


class TestLoadStyleCorpus:
    async def test_empty_dir(self, style_dir: Path) -> None:
        samples, sources, source_types = await load_style_corpus()
        assert samples == []
        assert sources == []
        assert source_types == []

    async def test_loads_md_files(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("This is my writing style.")
        samples, sources, source_types = await load_style_corpus()
        assert len(samples) == 1
        assert sources == ["essay.md"]
        assert source_types == ["prose"]

    async def test_loads_txt_files(self, style_dir: Path) -> None:
        (style_dir / "notes.txt").write_text("Some notes here.")
        samples, sources, source_types = await load_style_corpus()
        assert len(samples) == 1
        assert sources == ["notes.txt"]
        assert source_types == ["prose"]

    async def test_skips_urls_txt(self, style_dir: Path) -> None:
        (style_dir / "urls.txt").write_text("https://example.com\n")
        (style_dir / "real.md").write_text("Real content.")
        samples, sources, _types = await load_style_corpus()
        assert "urls.txt" not in sources
        assert "real.md" in sources

    async def test_loads_all_samples(self, style_dir: Path) -> None:
        for i in range(10):
            (style_dir / f"sample_{i:02d}.md").write_text(f"Sample {i}")
        samples, _, _types = await load_style_corpus()
        assert len(samples) == 10

    async def test_preserves_full_content(self, style_dir: Path) -> None:
        (style_dir / "long.md").write_text("x" * 5000)
        samples, _, _types = await load_style_corpus()
        assert len(samples[0]) == 5000

    async def test_loads_prescriptive_subdir(self, style_dir: Path) -> None:
        presc_dir = style_dir / "prescriptive"
        presc_dir.mkdir()
        (presc_dir / "rules.md").write_text("Never use em dashes.")
        (style_dir / "essay.md").write_text("My essay.")
        samples, sources, source_types = await load_style_corpus()
        assert len(samples) == 2
        prose_idx = sources.index("essay.md")
        presc_idx = sources.index("prescriptive/rules.md")
        assert source_types[prose_idx] == "prose"
        assert source_types[presc_idx] == "prescriptive"

    async def test_nonexistent_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "inkwell.agent.config.settings",
            type("S", (), {"style_corpus_path": "/nonexistent/path"})(),
        )
        samples, sources, source_types = await load_style_corpus()
        assert samples == []
        assert sources == []
        assert source_types == []


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
        voice, prescriptive = list_style_references()
        assert voice == []
        assert prescriptive == []

    def test_lists_files_and_urls(self, style_dir: Path) -> None:
        (style_dir / "essay.md").write_text("Content")
        (style_dir / "urls.txt").write_text("https://example.com\n")

        voice, _prescriptive = list_style_references()
        kinds = {e.kind for e in voice}
        assert "file" in kinds
        assert "url" in kinds

    def test_entry_types(self, style_dir: Path) -> None:
        (style_dir / "test.md").write_text("hello")
        voice, _prescriptive = list_style_references()
        assert len(voice) == 1
        assert isinstance(voice[0], StyleEntry)
        assert voice[0].kind == "file"
        assert voice[0].name == "test.md"
        assert voice[0].size > 0

    def test_lists_prescriptive_entries(self, style_dir: Path) -> None:
        presc_dir = style_dir / "prescriptive"
        presc_dir.mkdir()
        (presc_dir / "guide.md").write_text("No em dashes.")
        _voice, prescriptive = list_style_references()
        assert len(prescriptive) == 1
        assert prescriptive[0].name == "guide.md"


# ---------------------------------------------------------------------------
# Format-specific examples
# ---------------------------------------------------------------------------


class TestLoadFormatExamples:
    async def test_no_format_dir(self, style_dir: Path) -> None:
        samples, sources = await load_format_examples("lesswrong")
        assert samples == []
        assert sources == []

    async def test_loads_format_files(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "memo"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "good-memo.md").write_text("TO: Leadership\nFROM: Author")
        samples, sources = await load_format_examples("memo")
        assert len(samples) == 1
        assert sources == ["good-memo.md"]

    async def test_skips_urls_txt(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "blog"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "urls.txt").write_text("https://example.com/post\n")
        (fmt_dir / "example.md").write_text("Blog content.")
        samples, sources = await load_format_examples("blog")
        assert "urls.txt" not in sources
        assert "example.md" in sources

    async def test_handles_custom_format_prefix(self, style_dir: Path) -> None:
        fmt_dir = style_dir / "formats" / "custom"
        fmt_dir.mkdir(parents=True)
        (fmt_dir / "example.md").write_text("Custom format content.")
        samples, _ = await load_format_examples("custom:academic abstract")
        assert len(samples) == 1


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
        body, flag = parse_prescriptive_flag("Just a regular analysis.")
        assert body == "Just a regular analysis."
        assert not flag

    def test_prescriptive_true(self) -> None:
        text = "---\nprescriptive: true\n---\nBrief summary."
        body, flag = parse_prescriptive_flag(text)
        assert body == "Brief summary."
        assert flag

    def test_prescriptive_false(self) -> None:
        text = "---\nprescriptive: false\n---\nFull voice analysis."
        body, flag = parse_prescriptive_flag(text)
        assert body == "Full voice analysis."
        assert not flag

    def test_case_insensitive(self) -> None:
        text = "---\nPrescriptive: True\n---\nContent."
        _body, flag = parse_prescriptive_flag(text)
        assert flag

    def test_missing_closing_fence(self) -> None:
        text = "---\nprescriptive: true\nNo closing fence."
        body, flag = parse_prescriptive_flag(text)
        assert body == text
        assert not flag

    def test_empty_body_returns_original(self) -> None:
        text = "---\nprescriptive: true\n---\n"
        body, flag = parse_prescriptive_flag(text)
        assert body == text
        assert flag


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

        label, analysis, is_prescriptive = await analyze_single_source(text, "test-sample")
        assert "dry humor" in analysis
        assert not is_prescriptive

    @pytest.mark.asyncio
    async def test_analyze_single_source_caches_prescriptive(self, style_dir: Path) -> None:
        text = "Never use em dashes. Always use active voice."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text(
            "---\nprescriptive: true\n---\nBrief summary of rules."
        )

        label, analysis, is_prescriptive = await analyze_single_source(text, "rules")
        assert is_prescriptive
        assert "summary" in analysis

    @pytest.mark.asyncio
    async def test_analyze_single_source_old_cache_no_frontmatter(self, style_dir: Path) -> None:
        text = "Legacy cached analysis without frontmatter."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text("Old analysis without YAML frontmatter.")

        label, analysis, is_prescriptive = await analyze_single_source(text, "legacy")
        assert analysis == "Old analysis without YAML frontmatter."
        assert not is_prescriptive

    @pytest.mark.asyncio
    async def test_analyze_single_source_calls_query_on_miss(self, style_dir: Path) -> None:
        text = "New text to analyze."
        key = voice_cache_key(text)
        cache_path = voice_cache_dir() / f"{key}.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            cache_path.write_text("---\nprescriptive: false\n---\nFresh analysis result.")
            return type("C", (), {"text": ""})()

        with patch("inkwell.agent.tools.voice.query", new_callable=AsyncMock, side_effect=fake_query):
            label, analysis, is_prescriptive = await analyze_single_source(text, "new-sample")

        assert analysis == "Fresh analysis result."
        assert not is_prescriptive

    @pytest.mark.asyncio
    async def test_analyze_single_source_label_identity(self, style_dir: Path) -> None:
        """Labels travel with results — the core identity bug fix."""
        text = "Some writing sample."
        cache = voice_cache_dir()
        key = voice_cache_key(text)
        cache_path = cache / f"{key}.md"
        cache_path.write_text("---\nprescriptive: false\n---\nAnalysis.")

        label, analysis, is_prescriptive = await analyze_single_source(text, "my-label")
        assert label == "my-label"
        assert "Analysis" in analysis


class TestMergeVoiceAnalyses:
    @pytest.mark.asyncio
    async def test_merge_combines_analyses(self, tmp_path: Path) -> None:
        output_path = tmp_path / "voice.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            output_path.write_text("Merged guide content.")
            return type("C", (), {"text": ""})()

        with patch("inkwell.agent.tools.voice.query", new_callable=AsyncMock, side_effect=fake_query):
            result = await merge_voice_analyses(
                [("source-1", "Analysis 1"), ("source-2", "Analysis 2")],
                output_path,
            )
        assert result == "Merged guide content."

    @pytest.mark.asyncio
    async def test_merge_includes_format_examples(self, tmp_path: Path) -> None:
        output_path = tmp_path / "voice.md"

        async def fake_query(*args: object, **kwargs: object) -> object:
            output_path.write_text("Guide with format awareness.")
            return type("C", (), {"text": ""})()

        with patch("inkwell.agent.tools.voice.query", new_callable=AsyncMock, side_effect=fake_query) as mock_query:
            await merge_voice_analyses(
                [("source-1", "Analysis 1")],
                output_path,
                format_examples=[("good-post.md", "Example LW post content")],
            )
        prompt = mock_query.call_args[0][0]
        assert "merge_input.md" in prompt
        merge_input = Path(voice_cache_dir() / "merge_input.md").read_text(encoding="utf-8")
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
