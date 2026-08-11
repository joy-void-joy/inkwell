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
from collections.abc import Awaitable, Callable
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
from inkwell.agent.session import (
    AuthorComment,
    CommentIntake,
    CommentLedger,
    WritingSessionState,
)
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

        with session_state.seen_comments.recording(inp.comment_id):
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
    comment_ids: list[str] = Field(
        default_factory=list,
        description="The comments this batch was handed, claimed until recorded",
    )


class ClaimHold:
    """The comment claims the turns of one watcher are holding.

    A claim exists to keep the next poll off a comment already in flight, so
    no claim may outlive the turn that was supposed to write that comment
    down. A turn ends three ways that record nothing — the classifier raised,
    the turn itself failed, or the agent never reached one comment of its
    batch — and all three end here, where releasing puts the comment back in
    front of the next poll instead of stranding it for the life of the
    process. An id the turn did record left the claim set when it was marked,
    so releasing what is left over is exactly right.
    """

    def __init__(self, ledger: CommentLedger) -> None:
        self.ledger = ledger
        self.holding: tuple[str, ...] = ()

    def hold(self, comment_ids: list[str]) -> None:
        """Take over the claims a poll took out, alongside any still held.

        A batch can be replaced before its turn runs — wakes inside the
        debounce window coalesce — so holds accumulate rather than displace:
        the ids of a batch that never became a turn are released by the turn
        that ran instead of it.
        """
        self.holding = (*self.holding, *comment_ids)

    def release(self) -> None:
        """Drop every claim outstanding, whatever the turn did or did not do."""
        for comment_id in self.holding:
            self.ledger.release(comment_id)
        self.holding = ()


type CommentReader = Callable[[], Awaitable[CommentIntake]]
"""Polls whichever document's comments a watcher is watching."""

type UnreachableReport = Callable[[str], Awaitable[None]]
"""Tells the author a poll could not read the document it watches."""


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
    loop, and the agent's lifetime with it, so stopping one stops both — and
    with it the claims each batch was handed, which a stopped watch drops
    rather than leaving in front of a poll that will never come.
    """

    def __init__(
        self,
        agent: BackgroundAgent[CommentBatch, None],
        read_comments: CommentReader,
        report_unreachable: UnreachableReport,
        claims: ClaimHold,
        heading: str,
        poll_interval: float,
    ) -> None:
        self.agent = agent
        self.read_comments = read_comments
        self.report_unreachable = report_unreachable
        self.claims = claims
        self.heading = heading
        self.poll_interval = poll_interval
        self.poller: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the agent, then the loop that wakes it."""
        await self.agent.start()
        self.poller = asyncio.create_task(self.poll_forever())

    async def poll_forever(self) -> None:
        """Wake the agent for every poll that finds something.

        A poll that could not read the document is reported to the author
        rather than passed over: they are the one waiting on the comment it
        failed to read, and the next poll asks again.
        """
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                intake = await self.read_comments()
            except Exception:
                # A poll reports an unreadable document in its result, so
                # anything raised here is the poller itself failing. The watch
                # outlives it, and says so, rather than ending in a dead task.
                logger.exception("Polling for comments failed")
                await self.report_unreachable("the poller failed; see the run log")
                continue
            if not intake.reached:
                await self.report_unreachable(intake.unreachable)
            elif intake.comments:
                batch = CommentBatch(
                    prompt=batch_prompt(self.heading, intake.comments),
                    comment_ids=[comment["comment_id"] for comment in intake.comments],
                )
                self.claims.hold(batch.comment_ids)
                self.agent.wake(batch)

    async def stop(self) -> None:
        """Stop polling first, so no wake outlives the agent."""
        if self.poller is not None:
            self.poller.cancel()
            self.poller = None
        await self.agent.stop()
        self.claims.release()


def watcher_agent(
    *,
    name: str,
    system_prompt: str,
    tools: list[LupMcpTool],
    claims: ClaimHold,
) -> BackgroundAgent[CommentBatch, None]:
    """Build the background agent behind a watcher, over one tool server.

    Every turn ends in one of the two handlers below, which is why the claims
    a batch was handed are released there: whatever the turn recorded is
    already accounted for, and whatever it did not belongs back in front of
    the next poll.
    """
    server = create_mcp_server(name, tools=tools)

    async def done(_result: TurnResult[None]) -> None:
        """The work is the tool calls; the turn's prose is not read."""
        claims.release()

    async def report(error: TurnError) -> None:
        logger.error("%s turn failed: %s", name, error)
        claims.release()

    return BackgroundAgent(
        factory=provider_factory(
            model=stage_model("classify"),
            system_prompt=system_prompt,
            tool_servers={name: server},
            allowed_tools=[f"mcp__{name}__{tool.name}" for tool in tools],
            autonomy="unattended",
        ),
        state_to_request=lambda batch: turn_request(batch.prompt),
        result_handler=done,
        error_handler=report,
        config=BackgroundConfig(debounce_seconds=5.0),
    )


def create_comment_watcher(
    *,
    session_state: WritingSessionState,
    notes: PipelineNotes,
    plan_breaking_signal: asyncio.Event,
    report_unreachable: UnreachableReport,
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
        report_unreachable: Tells the author a poll could not read the document.
        poll_interval: Seconds between polls (default 30).
    """
    claims = ClaimHold(session_state.seen_comments)
    return PollingWatcher(
        agent=watcher_agent(
            name="watcher",
            system_prompt=WATCHER_SYSTEM_PROMPT,
            tools=create_watcher_tools(
                session_state=session_state,
                notes=notes,
                plan_breaking_signal=plan_breaking_signal,
            ),
            claims=claims,
        ),
        read_comments=session_state.poll_author_comments,
        report_unreachable=report_unreachable,
        claims=claims,
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

        with session_state.seen_source_comments.recording(inp.comment_id):
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
    report_unreachable: UnreachableReport,
    poll_interval: float = 30.0,
) -> PollingWatcher:
    """Watch the original source document for author comments.

    Polls for new comments on the source Google Doc, classifies them,
    acknowledges them, and writes to PipelineNotes. The same feedback loop as
    the output-document watcher, on the source material instead.
    """
    claims = ClaimHold(session_state.seen_source_comments)
    return PollingWatcher(
        agent=watcher_agent(
            name="source-watcher",
            system_prompt=SOURCE_WATCHER_SYSTEM_PROMPT,
            tools=create_source_watcher_tools(
                session_state=session_state,
                notes=notes,
                plan_breaking_signal=plan_breaking_signal,
            ),
            claims=claims,
        ),
        read_comments=session_state.poll_source_comments,
        report_unreachable=report_unreachable,
        claims=claims,
        heading="New source document comments",
        poll_interval=poll_interval,
    )
