"""The verdict vocabulary every kernel module returns."""

from typing import Literal


type DecisionEffect = Literal["allow", "ask", "deny", "defer"]

type SandboxPlacement = Literal["inside", "ambient", "escalable", "outside"]
"""Where a call runs, which is a different question from who decides it.

The four effects answer *who decides*; this answers *where it runs*. They are
two fields rather than one widened vocabulary because folding them together
forces an ask-plus-escape member next, and then a deny-plus-escape. Composed,
the pairs that carry meaning read:

* ``ask`` + ``outside`` — ask, warning the call will run out of the sandbox
* ``ask`` + ``escalable`` — ask, and once granted the caller may take it out
* ``deny`` — deny, whatever the placement says
* ``allow`` + ``inside`` — run confined, whatever the session's own mode is
* ``allow`` + ``ambient`` — run, deferring to the session's sandbox status
* ``allow`` + ``escalable`` — run confined, and the caller may take it out
* ``allow`` + ``outside`` — run out of the sandbox, unprompted

``escalable`` is not ``ambient`` said differently, and standing one in for the
other is invisible exactly where it matters. ``ambient`` reads the placement
off the session, so an unconfined session runs the call outside; ``escalable``
confines it whatever the session is doing and hands the choice to the agent
making the call. The two agree only while the session is already confined.

``ambient`` is the default because saying nothing about placement is what
almost every verdict means, and a runtime that cannot place a single call
renders the plain effect instead — see :meth:`KernelDecision.placed`.
"""

# lup: ignore[constant-declaration] — the words this gate says, in a kernel
# compiled hermetically into a bare dispatcher that takes no arguments
SANDBOX_ESCAPE_NOTICE = " — this will run outside the sandbox"
"""What an approval question adds when the call it approves also escapes."""

# lup: ignore[constant-declaration] — the offer's own wording, which every
# runtime carries unchanged because the reason text is the one channel they
# share; a caller replacing it would be replacing the offer, not configuring it
SANDBOX_ESCALATION_OFFER = (
    " — you may re-issue this outside the sandbox if it needs to be there"
)
"""How a permission to escalate reaches the agent that holds it.

The reason is the channel because every runtime carries reason text unchanged,
where a surfaced native option exists only where a runtime has one. It says
that the call may leave, not the words for leaving: those are one runtime's own
spelling, and prose reaches the agent with them from the spellings seam.

It also says nothing about where the call runs without leaving. That is the
placement's answer and not this text's, and stating it here would be the same
substitution the placement exists to prevent: on a runtime that renders no
placement the call follows the session, so prose promising confinement would
be false in exactly the unconfined session that matters.
"""

# lup: ignore[constant-declaration] — what that same offer degrades to, which
# has to say the outcome rather than the cause because two different absences
# reach it and the agent can act on neither
SANDBOX_ESCALATION_UNSUPPORTED = (
    " — the escalation offered here is not available, because nothing in this"
    " session takes a single call out of the sandbox"
)
"""What a permission to escalate degrades to where the agent cannot spend it.

The offer withdrawn and the gap stated. Two different absences reach it — a
runtime that gives the agent no words for leaving, and a session whose host
refuses an unsandboxed command however it is asked for — and the agent can act
on neither, so the wording names the outcome rather than the cause. Dropped in
silence it would read as an offer, and an agent that spends a turn finding out
otherwise learns nothing it can act on.
"""

# lup: ignore[constant-declaration] — the refusal a trapped call is stopped
# with, naming the bare filesystem error it would otherwise die on; the whole
# value of the words is that they are the same ones every time
SANDBOX_TRAPPED_REASON = (
    "this has to run outside the sandbox, but this call has no active per-call"
    " escape — resubmit through the runtime's native sandbox escalation when"
    " available; only a runtime without that channel needs a session that is"
    " not sandboxed. Running this call confined would fail with a bare"
    " read-only-filesystem error and misreport the boundary as repository failure"
)
"""Why a call that has to escape is refused where nothing can carry it out.

An intent no runtime will honour is worse than a refusal: the call runs,
fails on whatever it happened to write first, and the agent cannot tell that
from a repository in a bad state, so it retries, works around it, or reports
success from a session that never ran a command.
"""


def escalation_offer(sandbox: SandboxPlacement, reason: str) -> str:
    """What a verdict says to the agent rather than about it, if anything.

    A permission channel's reason reaches whoever was asked, and that is never
    the agent: on a grant nobody was asked and it reaches the record, and on
    an approval question a human reads it. So an offer addressed to the agent
    making the call has to say itself again on the channel an agent reads, and
    the escalable placement carries the only such offer — everything else a
    verdict says about a call it permits is bookkeeping, and a context line
    per permitted call is how a channel meant for what matters stops being
    read.

    The effect is not part of the question, because neither of the effects a
    placement survives puts this reason in front of the agent: it reaches the
    record on one and a human on the other. One function because four
    boundaries deliver it — both hook factories, the in-process renderer, and
    the compiled dispatcher — and a condition spelled out at each is one that
    can be spelled differently at each.
    """
    return reason if sandbox == "escalable" else ""


def sandbox_escaped(sandbox: SandboxPlacement, agent_escaped: bool) -> bool:
    """Whether a placed call runs outside, given what the call already asked.

    ``outside`` leaves because the verdict says so and ``inside`` stays
    whatever the call said, so only ``escalable`` reads the second argument:
    the permission is the agent's to spend, so a call that spent it goes out
    and one that did not stays confined. Answering a plain ``False`` there
    would answer for the agent and make the offer a verdict it has no way to
    accept — granted on the permission channel and revoked on the rewrite.

    Which field of a call carries the escape is one runtime's own spelling,
    so this takes the answer rather than the call and stays as neutral as the
    kernel around it. One function because two boundaries render the rewrite —
    the in-process seam and the compiled dispatcher — and a condition spelled
    out at each is one that can be spelled differently at each, which is how
    the offer came to be honoured on one path and stripped on the other.
    """
    return sandbox == "outside" or (sandbox == "escalable" and agent_escaped)


# lup: ignore[library-default] — the stdlib the kernel actually imports; the hermetic guarantee it exists to hold
KERNEL_IMPORT_ALLOWLIST = (
    "ast",
    "collections.abc",
    "fnmatch",
    "io",
    "pathlib",
    "posixpath",
    "re",
    "tokenize",
    "typing",
    "urllib.parse",
)
# The five below are sentences and one sentinel the kernel's own decisions
# carry: each is declared beside the verdict that returns it, so a caller
# passing different words would be returning a different verdict.
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
ESCALATE_HINT = (
    " — reshape the command into the allowed vocabulary, or resubmit with a"
    " leading '# lup: escalate: <why>' line to request approval"
)
RESHAPE_HINT = " — reshape the command into the allowed vocabulary"  # lup: ignore[constant-declaration] — refusal wording
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
SUBSTITUTION_REASON = (
    "command substitution is denied — run the inner command in its own call"
    " and splice its literal output, or read it through <(...) or a pipe"
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
BACKTICK_REASON = (
    "backtick substitution is denied — use $(...) so the inner command"
    " can be classified"
)
# lup: ignore[constant-declaration] — a spelling chosen to sit outside identifier
# space, which is the property the substitution proof below rests on
SUBSTITUTION_SENTINEL = "$~sub~"
"""Spliced into a word where a real ``$(...)`` stood.

The spelling sits outside identifier space, so no variable binding can
instantiate it, and a quoted literal that happens to match only makes the
word read as opaque — the conservative direction.
"""


class KernelDecision:
    """Dependency-free allow, ask, deny, or defer result, and where it runs."""

    effect: DecisionEffect
    reason: str
    sandbox: SandboxPlacement

    def __init__(
        self,
        effect: DecisionEffect,
        reason: str = "",
        sandbox: SandboxPlacement = "ambient",
    ) -> None:
        if effect not in ("allow", "ask", "deny", "defer"):
            raise ValueError(f"invalid kernel decision effect {effect!r}")
        if sandbox not in ("inside", "ambient", "escalable", "outside"):
            raise ValueError(f"invalid kernel decision placement {sandbox!r}")
        self.effect = effect
        self.reason = reason
        # Only a verdict this policy actually reached is placed: a refusal is
        # not softened by where the call would have run, and a deferral hands
        # the whole question over, the session's sandbox status included.
        self.sandbox = sandbox if effect in ("allow", "ask") else "ambient"

    def placed(self, escapable: bool, agent_escalates: bool) -> "KernelDecision":
        """This verdict as the runtime about to render it will carry it out.

        The two facts are two questions, and a runtime may answer them
        differently. ``escapable`` is whether *this verdict* can put the call
        outside — the channel a rendered placement needs, and what ``outside``
        is asking for. ``agent_escalates`` is whether *the agent making the
        call* can put its own call outside, which is what ``escalable`` offers
        and which needs no channel here at all, since the offer travels as
        reason text. Answering both from one flag is what makes a runtime with
        one and not the other unrepresentable.

        Where a verdict cannot be placed it renders the plain effect: an
        intent the runtime will not honour must not be spelled, or the verdict
        reads as escaped while the call runs confined.

        Two pairs say something the effect does not say by itself. An approval
        question over a call that also escapes asks the human two things, so
        the reason says both. And a permission to escalate stands or falls on
        whether the agent can spend it: withdrawn, it becomes the plain
        confined behaviour and says ``inside``, which is that behaviour spelled
        rather than ``ambient``, which would hand the placement back to the
        session the offer was never reading. Either way the reason states which
        it got, and either placement reaches the wire only where the runtime
        has the channel — where it has none the call follows the session, which
        is why neither reason claims confinement in words.

        Neither reaches a refusal: a deny or a defer arrives here with its
        placement already collapsed, so no reason gains an offer that the
        verdict does not extend.
        """
        if self.sandbox == "escalable" and not agent_escalates:
            reason = self.reason + SANDBOX_ESCALATION_UNSUPPORTED
            return KernelDecision(
                self.effect, reason, "inside" if escapable else "ambient"
            )
        if self.sandbox == "escalable":
            reason = self.reason + SANDBOX_ESCALATION_OFFER
            return KernelDecision(
                self.effect, reason, "escalable" if escapable else "ambient"
            )
        if not escapable:
            return KernelDecision(self.effect, self.reason)
        if self.effect == "ask" and self.sandbox == "outside":
            return KernelDecision("ask", self.reason + SANDBOX_ESCAPE_NOTICE, "outside")
        return self


def unjudged(reason: str) -> KernelDecision:
    """One machinery bail-out: the kernel cannot judge, so it defers.

    The shell boundary decides what no-judgment means: a sandboxed
    execution runs confined by the OS, an unsandboxed one converts to a
    deny naming the escalation recipe.
    """
    return KernelDecision("defer", reason)
