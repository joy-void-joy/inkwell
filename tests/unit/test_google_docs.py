"""Tests for Google Docs helper behavior: non-fatal degradation."""

import httplib2
import pytest

from inkwell.agent.google_auth import GoogleAuthError
from inkwell.agent.tools.google_docs import gdoc_nonfatal
from lup.mcp import ToolError


class TestGdocNonfatal:
    async def test_swallows_auth_error_so_a_dead_token_does_not_crash(self) -> None:
        async with gdoc_nonfatal("write final tab"):
            raise GoogleAuthError("Token has been expired or revoked")
        # Reaching here means the auth lapse degraded to a logged warning
        # instead of crashing the stage.

    async def test_swallows_tool_error(self) -> None:
        async with gdoc_nonfatal("create tab"):
            raise ToolError("transient Doc failure")

    async def test_swallows_a_name_that_would_not_resolve(self) -> None:
        # The transport's failures are not OSErrors — they descend straight
        # from Exception — so a machine that lost its network mid-run used to
        # take the run down through the one boundary that exists to stop a
        # display surface doing exactly that.
        async with gdoc_nonfatal("sync draft output.md"):
            raise httplib2.ServerNotFoundError(
                "Unable to find the server at docs.googleapis.com"
            )

    async def test_swallows_a_connection_that_dropped(self) -> None:
        async with gdoc_nonfatal("write final tab"):
            raise httplib2.HttpLib2Error("connection reset by peer")

    async def test_reraises_unexpected_errors(self) -> None:
        # The safety net is for display-surface failures, not logic bugs:
        # a genuine error must still propagate.
        with pytest.raises(ValueError):
            async with gdoc_nonfatal("write"):
                raise ValueError("a real bug")
