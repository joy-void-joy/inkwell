"""Conversation extraction tool — extracts Claude conversations from share links.

Ported from /home/pfftz/kernel/extractors/claude.py. Converts Claude.ai
share link conversations into structured markdown with speaker tags.
"""

import logging

import httpx
from pydantic import BaseModel, Field

from inkwell.agent.config import settings
from lup.mcp import ToolError, lup_tool

logger = logging.getLogger(__name__)

CLAUDE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": "https://claude.ai",
    "Connection": "keep-alive",
}


class ExtractConversationInput(BaseModel):
    url: str = Field(
        description="Claude.ai share URL (e.g., https://claude.ai/share/...)"
    )


class ConversationMessage(BaseModel):
    speaker: str = Field(description="'user' or 'claude'")
    text: str = Field(description="Message content")


class ExtractConversationOutput(BaseModel):
    url: str = Field(description="Original share URL")
    messages: list[ConversationMessage] = Field(description="Extracted messages")
    markdown: str = Field(description="Full conversation as markdown with speaker tags")
    message_count: int = Field(description="Number of messages extracted")


def extract_share_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def message_text(msg: dict[str, object]) -> str:
    content = msg.get("content", [])
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return "\n\n".join(parts)


async def fetch_snapshot(share_id: str) -> dict[str, object]:
    """Fetch conversation snapshot, trying public API first then org API."""
    public_url = (
        f"https://claude.ai/api/chat_snapshots/{share_id}"
        "?rendering_mode=messages&render_all_tools=true"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(public_url, headers=CLAUDE_HEADERS)
        if resp.status_code == 200:
            result: dict[str, object] = resp.json()
            return result

        cookie = settings.claude_cookie
        if not cookie:
            raise ToolError(
                f"Share link requires authentication (status {resp.status_code}). "
                "Set CLAUDE_COOKIE in .env.local"
            )

        auth_headers = {**CLAUDE_HEADERS, "Cookie": cookie}

        org_uuid = settings.claude_org_uuid
        if not org_uuid:
            org_resp = await client.get(
                "https://claude.ai/api/organizations", headers=auth_headers
            )
            if org_resp.status_code != 200:
                raise ToolError(
                    f"Failed to fetch organizations (status {org_resp.status_code})"
                )
            orgs: list[dict[str, str]] = org_resp.json()
            if not orgs:
                raise ToolError("No organizations found for this account")
            org_uuid = orgs[0].get("uuid", "")

        org_url = (
            f"https://claude.ai/api/organizations/{org_uuid}/chat_snapshots/{share_id}"
            "?rendering_mode=messages&render_all_tools=true"
        )
        auth_headers["Referer"] = f"https://claude.ai/share/{share_id}"

        resp = await client.get(org_url, headers=auth_headers)
        if resp.status_code != 200:
            raise ToolError(f"Failed to fetch conversation (status {resp.status_code})")
        result = resp.json()
        return result


@lup_tool(
    "Extract a Claude.ai conversation from a share link. Converts the conversation "
    "into structured markdown with <user> and <claude> speaker tags. Use this as "
    "the first step when the author provides a Claude conversation as source "
    "material. Returns both structured messages and formatted markdown. "
    "Requires CLAUDE_COOKIE in .env.local for org-restricted share links."
)
async def extract_conversation(
    params: ExtractConversationInput,
) -> ExtractConversationOutput:
    share_id = extract_share_id(params.url)
    data = await fetch_snapshot(share_id)

    chat_messages = data.get("chat_messages", [])
    if not isinstance(chat_messages, list):
        raise ToolError("Unexpected response format: no chat_messages array")

    messages: list[ConversationMessage] = []
    md_parts: list[str] = []

    for msg in chat_messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender", "")
        if not isinstance(sender, str):
            continue
        text = message_text(msg)
        if not text.strip():
            continue

        speaker = "user" if sender == "human" else "claude"
        messages.append(ConversationMessage(speaker=speaker, text=text))
        md_parts.append(f"<{speaker}>\n{text}\n</{speaker}>")

    if not messages:
        raise ToolError("No messages found in conversation")

    return ExtractConversationOutput(
        url=params.url,
        messages=messages,
        markdown="\n\n".join(md_parts),
        message_count=len(messages),
    )


EXTRACT_TOOLS = [extract_conversation]
