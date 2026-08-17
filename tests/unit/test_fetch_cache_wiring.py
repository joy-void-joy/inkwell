"""What the extractors ask the network for a second time, and what they do not.

The cache itself is the library's and is tested there. What matters here is the
wiring: that each extractor consults it, that the things which must never be
cached are refused before they reach it, and that two extractors asking for one
URL cost one transfer between them.
"""

from collections.abc import Iterator
from functools import partial
from pathlib import Path

import httpx
import pytest
from lup.mcp import ToolError
from lup.workspace.content_safety import ContentSafetyConfig
from lup.workspace.content_safety import state as content_state
from pydantic import BaseModel

from inkwell.agent.tools.extract import ExtractBatchInput, extract_webpage_batch
from inkwell.agent.tools.research import fetch as fetch_module
from inkwell.agent.tools.research.fetch import do_fetch_source

URL = "https://example.com/article"
PAGE = (
    "<html><head><title>A Title</title></head><body>"
    "<article><p>" + ("Real article prose that trafilatura will keep. " * 20) + "</p>"
    "</article></body></html>"
)


@pytest.fixture(autouse=True)
def content_in_tmp(tmp_path: Path) -> Iterator[None]:
    """Keep saved content out of the working copy for these tests."""
    content_state.config = ContentSafetyConfig(directory=tmp_path / "content")
    yield
    content_state.config = None


class Hits(BaseModel):
    """How many times the network was actually reached, and for what."""

    urls: list[str] = []

    def record(self, url: str) -> None:
        self.urls.append(url)

    @property
    def count(self) -> int:
        return len(self.urls)


def serving(
    monkeypatch: pytest.MonkeyPatch,
    hits: Hits,
    *,
    status: int = 200,
    body: str | bytes = PAGE,
    content_type: str = "text/html; charset=utf-8",
) -> None:
    """Answer every request from this fixture, counting the ones that arrive."""

    def handler(request: httpx.Request) -> httpx.Response:
        hits.record(str(request.url))
        payload = body.encode() if isinstance(body, str) else body
        return httpx.Response(
            status, content=payload, headers={"content-type": content_type}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )


class TestFetchSource:
    async def test_a_second_fetch_of_one_url_never_reaches_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = Hits()
        serving(monkeypatch, hits)

        first = await do_fetch_source(URL)
        second = await do_fetch_source(URL)

        assert hits.count == 1
        assert Path(second.content.path).read_text(encoding="utf-8") == (
            Path(first.content.path).read_text(encoding="utf-8")
        )

    async def test_two_urls_are_two_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = Hits()
        serving(monkeypatch, hits)

        await do_fetch_source(URL)
        await do_fetch_source("https://example.com/other")

        assert hits.count == 2

    async def test_a_missing_page_is_never_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = Hits()
        serving(monkeypatch, hits, status=404, body="gone")

        for _attempt in range(2):
            with pytest.raises(ToolError):
                await do_fetch_source(URL)

        assert hits.count == 2

    async def test_an_anti_bot_page_is_never_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = Hits()
        serving(monkeypatch, hits, body="<html>Just a moment...</html>")

        for _attempt in range(2):
            with pytest.raises(ToolError):
                await do_fetch_source(URL)

        assert hits.count == 2


class TestPdf:
    async def test_a_pdf_is_written_from_the_cache_without_downloading_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hits = Hits()
        body = b"%PDF-1.4 pretend this is a paper"
        serving(monkeypatch, hits, body=body, content_type="application/pdf")
        monkeypatch.setattr(fetch_module, "DOWNLOADS_DIR", tmp_path / "downloads")

        first = await do_fetch_source(URL)
        second = await do_fetch_source(URL)

        assert hits.count == 1
        assert first.pdf_path is not None
        assert second.pdf_path == first.pdf_path
        assert Path(first.pdf_path).read_bytes() == body

    async def test_an_oversized_pdf_is_refused_and_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hits = Hits()
        oversized = b"%PDF" + b"x" * (fetch_module.MAX_PDF_BYTES + 1)
        serving(monkeypatch, hits, body=oversized, content_type="application/pdf")
        monkeypatch.setattr(fetch_module, "DOWNLOADS_DIR", tmp_path / "downloads")

        for _attempt in range(2):
            with pytest.raises(ToolError):
                await do_fetch_source(URL)

        assert hits.count == 2


class TestSharing:
    async def test_the_batch_extractor_reuses_what_fetch_source_downloaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hits = Hits()
        serving(monkeypatch, hits)

        await do_fetch_source(URL)
        batch = await extract_webpage_batch(ExtractBatchInput(urls=[URL]))

        assert hits.count == 1
        assert [result.url for result in batch.results] == [URL]
