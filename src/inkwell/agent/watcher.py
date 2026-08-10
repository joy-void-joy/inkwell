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
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field

from lup.runtime.background import BackgroundAgent, BackgroundConfig
from lup.runtime.errors import TurnError
from lup.runtime.models import TurnResult, turn_request
from inkwell.agent.client import provider_factory, query
from lup.mcp import LupMcpTool, ToolError, create_mcp_server, lup_tool

from inkwell.agent.config import stage_model
from inkwell.agent.models import ClassifiedComment, CommentImpact
from inkwell.agent.notes import PipelineNotes
from inkwell.agent.session import AuthorComment, WritingSessionState
from inkwell.agent.stages import COMMENT_CLASSIFIER_PROMPT
from inkwell.agent.tools.google_docs import do_reply_to_comment

logger = logging.getLogger(__name__)

ACKNOWLEDGE_TEMPLATES: dict[CommentImpact, str | None] = {
    "plan_breaking": (
        "Understood — this changes the direction. "
        "Restarting from the planning stage to incorporate your feedback."
    ),
    "stage_local": "Noted — will apply this in the next revision.",
    "clarification": "Got it, incorporating this.",
    "dismiss": None,
    "revert_suggested": None,
}
"""The reply each classification earns. `None` where the comment is answered by
acting on it — reverting the damage, or ignoring noise — not by posting."""

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
    reply: str = Field(
        default="", description="Reply text if responding to agent comment"
    )


class PollOutput(BaseModel):
    classified: ClassifiedComment


class AckInput(BaseModel):
    comment_id: str = Field(description="Comment ID to reply to")
    impact: CommentImpact = Field(description="The classified impact level")


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
        task = f"Classify this author comment:\n\nComment: {inp.content}\n"
        if inp.anchor_text:
            task += f'Anchored to: "{inp.anchor_text}"\n'
        if inp.reply:
            task += f"Reply to agent question: {inp.reply}\n"

        classified = await query(
            task,
            output_type=ClassifiedComment,
            model=stage_model("classify"),
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
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

        reply_text = ACKNOWLEDGE_TEMPLATES[inp.impact] or "Noted."
        try:
            await do_reply_to_comment(
                session_state.doc_id,
                inp.comment_id,
                reply_text,
                session_state=session_state,
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


class CommentBatch(BaseModel):
    """One poll's worth of comments, as the turn that processes them reads it."""

    prompt: str


type CommentReader = Callable[[], list[AuthorComment]]
"""Reads whichever document's new comments a watcher is watching."""


def batch_prompt(heading: str, comments: list[AuthorComment]) -> str:
    """Render one poll's comments as the turn's instruction."""
    lines = [f"{heading} ({len(comments)}):"]
    for comment in comments:
        lines.append("\n---")
        lines.append(f"Comment ID: {comment['comment_id']}")
        lines.append(f"Content: {comment['content']}")
        if comment["anchor_text"]:
            lines.append(f'Anchored to: "{comment["anchor_text"]}"')
        if comment["reply"]:
            lines.append(f"Reply: {comment['reply']}")
    lines.append(
        "\nProcess each comment: classify → acknowledge → signal if plan-breaking."
    )
    return "\n".join(lines)


class PollingWatcher:
    """A background agent woken by a poll rather than by a caller.

    ``BackgroundAgent`` runs one turn per wake and coalesces wakes inside its
    debounce window, which is exactly a watcher's execution shape. What it
    does not carry is *when* to wake, because that is the domain's question —
    here, every so often, if the document has new comments. This owns that
    loop, and the agent's lifetime with it, so stopping one stops both.
    """

    def __init__(
        self,
        agent: BackgroundAgent[CommentBatch, None],
        read_comments: CommentReader,
        heading: str,
        poll_interval: float,
    ) -> None:
        self.agent = agent
        self.read_comments = read_comments
        self.heading = heading
        self.poll_interval = poll_interval
        self.poller: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the agent, then the loop that wakes it."""
        await self.agent.start()
        self.poller = asyncio.create_task(self.poll_forever())

    async def poll_forever(self) -> None:
        """Wake the agent for every poll that finds something."""
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                comments = self.read_comments()
            except Exception:
                # One unreachable poll is not the end of the watch: the
                # document may be momentarily unavailable, and the next
                # poll asks again.
                logger.exception("Polling for comments failed")
                continue
            if comments:
                self.agent.wake(
                    CommentBatch(prompt=batch_prompt(self.heading, comments))
                )

    async def stop(self) -> None:
        """Stop polling first, so no wake outlives the agent."""
        if self.poller is not None:
            self.poller.cancel()
            self.poller = None
        await self.agent.stop()


def watcher_agent(
    *, name: str, system_prompt: str, tools: list[LupMcpTool]
) -> BackgroundAgent[CommentBatch, None]:
    """Build the background agent behind a watcher, over one tool server."""
    server = create_mcp_server(name, tools=tools)

    async def discard(_result: TurnResult[None]) -> None:
        """The work is the tool calls; the turn's prose is not read."""

    async def report(error: TurnError) -> None:
        logger.error("%s turn failed: %s", name, error)

    return BackgroundAgent(
        factory=provider_factory(
            model=stage_model("classify"),
            system_prompt=system_prompt,
            tool_servers={name: server},
            allowed_tools=[f"mcp__{name}__{tool.name}" for tool in tools],
            autonomy="unattended",
        ),
        state_to_request=lambda batch: turn_request(batch.prompt),
        result_handler=discard,
        error_handler=report,
        config=BackgroundConfig(debounce_seconds=5.0),
    )


def create_comment_watcher(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
    poll_interval: float = 30.0,
) -> PollingWatcher:
    """Watch the output document for author comments.

    Polls for new GDoc comments, classifies them, acknowledges them, and
    writes to PipelineNotes. Plan-breaking comments signal the pipeline state
    machine.

    Args:
        session_state: Shared session state with doc_id and comment tracking.
        notes: PipelineNotes instance for writing classified comments.
        plan_breaking_signal: asyncio.Event set when plan-breaking feedback arrives.
        poll_interval: Seconds between polls (default 30).
    """
    return PollingWatcher(
        agent=watcher_agent(
            name="watcher",
            system_prompt=WATCHER_SYSTEM_PROMPT,
            tools=create_watcher_tools(
                session_state=session_state,
                notes=notes,
                plan_breaking_signal=plan_breaking_signal,
            ),
        ),
        read_comments=session_state.get_new_author_comments_sync,
        heading="New comments",
        poll_interval=poll_interval,
    )


SOURCE_WATCHER_SYSTEM_PROMPT = """\
You are a source document watcher for a writing pipeline. The author's \
original source material is a Google Doc. Each turn you receive new \
comments from that source document. For each comment:

1. Call poll_and_classify to classify the comment's impact
2. Call acknowledge to reply on the source doc
3. If the comment is plan-breaking, call signal_restart

These comments are from the original source — not the output document. \
Treat them as author intent signals: the author is annotating their own \
material to guide what the pipeline should do with it.

Process ALL comments in each batch. Be decisive.\
"""


def create_source_watcher_tools(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
) -> list[LupMcpTool]:
    """Create MCP tools for the Source Document Watcher."""

    @lup_tool(
        "Classify a source document comment's impact on the pipeline. "
        "Uses a fast LLM call to determine impact level. "
        "Also writes the classified comment to the shared notes system.",
        name="poll_and_classify",
    )
    async def poll_and_classify(inp: PollInput) -> PollOutput:
        task = (
            f"Classify this author comment from the SOURCE document "
            f"(the original material being turned into an article):\n\n"
            f"Comment: {inp.content}\n"
        )
        if inp.anchor_text:
            task += f'Anchored to: "{inp.anchor_text}"\n'
        if inp.reply:
            task += f"Reply to agent question: {inp.reply}\n"

        classified = await query(
            task,
            output_type=ClassifiedComment,
            model=stage_model("classify"),
            system_prompt=COMMENT_CLASSIFIER_PROMPT,
            max_thinking_tokens=128_000 - 1,
            autonomy="unattended",
            prefix="[source-classify] ",
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
        "Mark a source document comment as acknowledged. Does NOT post a "
        "reply — the source document is read-only to the pipeline.",
        name="acknowledge",
    )
    async def acknowledge(inp: AckInput) -> AckOutput:
        return AckOutput(status="acknowledged")

    @lup_tool(
        "Signal the pipeline that plan-breaking feedback requires a restart. "
        "Call this after classifying a comment as plan_breaking.",
        name="signal_restart",
    )
    async def signal_restart(inp: SignalInput) -> SignalOutput:
        logger.info("Plan-breaking signal (source doc): %s", inp.reason)
        plan_breaking_signal.set()
        return SignalOutput()

    return [poll_and_classify, acknowledge, signal_restart]


def create_source_watcher(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
    poll_interval: float = 30.0,
) -> PollingWatcher:
    """Watch the original source document for author comments.

    Polls for new comments on the source Google Doc, classifies them,
    acknowledges them, and writes to PipelineNotes. The same feedback loop as
    the output-document watcher, on the source material instead.
    """
    return PollingWatcher(
        agent=watcher_agent(
            name="source-watcher",
            system_prompt=SOURCE_WATCHER_SYSTEM_PROMPT,
            tools=create_source_watcher_tools(
                session_state=session_state,
                notes=notes,
                plan_breaking_signal=plan_breaking_signal,
            ),
        ),
        read_comments=session_state.get_new_source_comments_sync,
        heading="New source document comments",
        poll_interval=poll_interval,
    )
