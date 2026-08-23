"""Exa returns a readable handle instead of overflowing the tool response."""

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import httpx
import pytest

from lup.workspace.content_safety import SavedContent
from inkwell.agent.tools.research import exa


def exa_payload(count: int) -> dict[str, object]:
    """A wire response with enough complete hits to exercise the inline page."""
    return {
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://example.com/{index}",
                "text": "complete text " * 60,
                "highlights": [f"highlight {index}"],
            }
            for index in range(count)
        ]
    }


def content_saver(root: Path) -> Callable[[str, str, str], SavedContent]:
    """A configured content store whose files remain available to assertions."""
    saved = 0

    def save(kind: str, key: str, content: str) -> SavedContent:
        nonlocal saved
        saved += 1
        path = root / f"{kind}-{saved}.json"
        path.write_text(content, encoding="utf-8")
        return SavedContent(
            path=str(path),
            word_count=len(content.split()),
            char_count=len(content),
            preview=content,
        )

    return save


async def test_large_result_set_is_complete_at_a_readable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=exa_payload(6))
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(exa.httpx, "AsyncClient", partial(client, transport=transport))
    monkeypatch.setattr(
        exa, "current_settings", lambda: SimpleNamespace(exa_api_key="test-key")
    )
    monkeypatch.setattr(exa, "save_content", content_saver(tmp_path))

    result = await exa.exa_search.call_handler(
        exa.ExaSearchInput(query="a query", num_results=6)
    )

    assert result.count == 6
    assert result.returned == 5
    assert result.results_path is not None
    complete = json.loads(Path(result.results_path).read_text(encoding="utf-8"))
    assert [one["title"] for one in complete] == [
        f"Result {index}" for index in range(6)
    ]
    assert all(one["full_text_path"] for one in complete)


async def test_unavailable_manifest_store_returns_every_result_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=exa_payload(6))
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(exa.httpx, "AsyncClient", partial(client, transport=transport))
    monkeypatch.setattr(
        exa, "current_settings", lambda: SimpleNamespace(exa_api_key="test-key")
    )
    save_result = content_saver(tmp_path)

    def save(kind: str, key: str, content: str) -> SavedContent:
        if kind == "exa-search":
            raise RuntimeError("not configured")
        return save_result(kind, key, content)

    monkeypatch.setattr(exa, "save_content", save)
    result = await exa.exa_search.call_handler(exa.ExaSearchInput(query="a query"))

    assert result.count == result.returned == 6
    assert result.results_path is None
