"""Shared test fixtures.

Add fixtures here that are used across multiple test files.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from lup.workspace.fetch_cache import FetchCache, state


@pytest.fixture(autouse=True)
def fetch_cache_in_tmp(tmp_path: Path) -> Iterator[None]:
    """Point the fetch cache at this test's own directory.

    The cache is deliberately shared across runs, which for a test means
    shared across tests and with the developer's real cache: without this, a
    test that fetches writes into the working copy, and the next test to ask
    for the same URL is answered by whatever the last one happened to store.
    """
    state.cache = FetchCache(directory=tmp_path / "fetch-cache")
    yield
    state.cache = None
