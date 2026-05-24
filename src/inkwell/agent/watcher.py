"""Comment Watcher — BackgroundAgent that monitors Google Doc comments.

Runs alongside the pipeline, continuously polling for new author comments.
When comments arrive:
1. Classifies impact (plan_breaking, stage_local, clarification)
2. Acknowledges with a Drive API reply
3. Writes to PipelineNotes
4. Signals the pipeline if plan-breaking feedback arrives
"""

import asyncio
import logging
from datetime import datetime

from pydantic import BaseModel, Field

from lup.background import BackgroundAgent
from lup.client import query
from lup.mcp import LupMcpTool, ToolError, extract_sdk_tools, lup_tool

from inkwell.agent.models import ClassifiedComment
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.session import WritingSessionState
from inkwell.agent.stages import COMMENT_CLASSIFIER_PROMPT
from inkwell.agent.tools.google_docs import do_reply_to_comment

logger = logging.getLogger(__name__)

ACKNOWLEDGE_TEMPLATES: dict[str, str] = {
    "plan_breaking": (
        "Understood — this changes the direction. "
        "Restarting from the planning stage to incorporate your feedback."
    ),
    "stage_local": "Noted — will apply this in the next revision.",
    "clarification": "Got it, incorporating this.",
}

WATCHER_SYSTEM_PROMPT = """\
You are a comment watcher for a writing pipeline. Each turn you receive \
new author comments from a Google Doc. For each comment:

1. Call poll_and_classify to classify the comment's impact
2. Call acknowledge to post a reply to the author
3. If the comment is plan-breaking, call signal_restart

Process ALL comments in each batch. Be decisive about classification — \
don't overthink it. The classifier prompt guides your judgment.

After processing all comments, wait for the next batch.\
"""


class PollInput(BaseModel):
    comment_id: str = Field(description="The comment ID to classify")
    content: str = Field(description="The comment text")
    anchor_text: str = Field(default="", description="Text the comment is anchored to")
    reply: str = Field(default="", description="Reply text if responding to agent comment")


class PollOutput(BaseModel):
    classified: ClassifiedComment


class AckInput(BaseModel):
    comment_id: str = Field(description="Comment ID to reply to")
    impact: str = Field(description="The classified impact level")


class AckOutput(BaseModel):
    status: str = Field(default="acknowledged")


class SignalInput(BaseModel):
    reason: str = Field(description="Brief description of why restart is needed")


class SignalOutput(BaseModel):
    status: str = Field(default="signaled")


def create_watcher_tools(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
) -> list[LupMcpTool]:
    """Create MCP tools for the Comment Watcher agent."""

    @lup_tool(
        "Classify a comment's impact on the pipeline. Uses a fast LLM call "
        "to determine whether the comment is plan-breaking (requires restart), "
        "stage-local (affects current work), or a clarification. "
        "Also writes the classified comment to the shared notes system.",
        name="poll_and_classify",
    )
    async def poll_and_classify(inp: PollInput) -> PollOutput:
        task = (
            f"Classify this author comment:\n\n"
            f"Comment: {inp.content}\n"
        )
        if inp.anchor_text:
            task += f"Anchored to: \"{inp.anchor_text}\"\n"
        if inp.reply:
            task += f"Reply to agent question: {inp.reply}\n"

        classified = await query(
            task,
            output_type=ClassifiedComment,
            model="claude-opus-4-6",
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            permission_mode="bypassPermissions",
            prefix="[classify] ",
        )
        if classified is None:
            raise ToolError("Classification failed — no output from classifier")

        classified.comment_id = inp.comment_id
        classified.content = inp.content
        classified.anchor_text = inp.anchor_text
        classified.reply = inp.reply
        classified.timestamp = datetime.now().isoformat()

        await notes.add_comment(classified)
        return PollOutput(classified=classified)

    @lup_tool(
        "Acknowledge a comment by posting a reply on the Google Doc. "
        "Uses a template reply based on the impact classification.",
        name="acknowledge",
    )
    async def acknowledge(inp: AckInput) -> AckOutput:
        if not session_state.doc_id:
            return AckOutput(status="no_doc")

        reply_text = ACKNOWLEDGE_TEMPLATES.get(inp.impact, "Noted.")
        try:
            await do_reply_to_comment(
                session_state.doc_id,
                inp.comment_id,
                reply_text,
            )
        except (RuntimeError, OSError):
            logger.warning("Failed to acknowledge comment %s", inp.comment_id)
            return AckOutput(status="failed")

        return AckOutput(status="acknowledged")

    @lup_tool(
        "Signal the pipeline that plan-breaking feedback requires a restart. "
        "Call this after classifying a comment as plan_breaking.",
        name="signal_restart",
    )
    async def signal_restart(inp: SignalInput) -> SignalOutput:
        logger.info("Plan-breaking signal: %s", inp.reason)
        plan_breaking_signal.set()
        return SignalOutput()

    return [poll_and_classify, acknowledge, signal_restart]


def create_comment_watcher(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
    poll_interval: float = 30.0,
) -> BackgroundAgent:
    """Create a Comment Watcher BackgroundAgent.

    The watcher polls for new GDoc comments, classifies them, acknowledges
    them, and writes to PipelineNotes. Plan-breaking comments trigger a
    signal to the pipeline state machine.

    Args:
        session_state: Shared session state with doc_id and comment tracking.
        notes: PipelineNotes instance for writing classified comments.
        plan_breaking_signal: asyncio.Event set when plan-breaking feedback arrives.
        poll_interval: Seconds between polls (default 30).
    """
    last_poll = [0.0]

    def build_message() -> str | None:
        import time

        now = time.time()
        if now - last_poll[0] < poll_interval:
            return None
        last_poll[0] = now

        new_comments = session_state.get_new_author_comments()
        if not new_comments:
            return None

        lines = [f"New comments ({len(new_comments)}):"]
        for c in new_comments:
            lines.append("\n---")
            lines.append(f"Comment ID: {c['comment_id']}")
            lines.append(f"Content: {c['content']}")
            if c["anchor_text"]:
                lines.append(f"Anchored to: \"{c['anchor_text']}\"")
            if c["reply"]:
                lines.append(f"Reply: {c['reply']}")

        lines.append(
            "\nProcess each comment: classify → acknowledge → signal if plan-breaking."
        )
        return "\n".join(lines)

    tools = create_watcher_tools(
        session_state=session_state,
        notes=notes,
        plan_breaking_signal=plan_breaking_signal,
    )

    return BackgroundAgent(
        name="watcher",
        system_prompt=WATCHER_SYSTEM_PROMPT,
        tools=extract_sdk_tools(tools),
        build_message=build_message,
        start_message="[Comment watcher started — monitoring Google Doc for author feedback]",
        model="claude-opus-4-6",
        debounce_seconds=5.0,
        allowed_tools=[
            "mcp__watcher__poll_and_classify",
            "mcp__watcher__acknowledge",
            "mcp__watcher__signal_restart",
        ],
    )
