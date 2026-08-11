"""The verdict vocabulary every kernel module returns."""

from typing import Literal


type DecisionEffect = Literal["allow", "ask", "deny", "defer"]


KERNEL_IMPORT_ALLOWLIST = (  # lup: ignore[library-default] — the stdlib the kernel actually imports; the hermetic guarantee it exists to hold
    "ast",
    "collections.abc",
    "io",
    "posixpath",
    "re",
    "tokenize",
    "typing",
    "urllib.parse",
)
ESCALATE_HINT = (
    " — reshape the command into the allowed vocabulary, or resubmit with a"
    " leading '# lup: escalate: <why>' line to request approval"
)
RESHAPE_HINT = " — reshape the command into the allowed vocabulary"
SUBSTITUTION_REASON = (
    "command substitution is denied — run the inner command in its own call"
    " and splice its literal output, or read it through <(...) or a pipe"
)
BACKTICK_REASON = (
    "backtick substitution is denied — use $(...) so the inner command"
    " can be classified"
)
SUBSTITUTION_SENTINEL = "$~sub~"
"""Spliced into a word where a real ``$(...)`` stood.

The spelling sits outside identifier space, so no variable binding can
instantiate it, and a quoted literal that happens to match only makes the
word read as opaque — the conservative direction.
"""


class KernelDecision:
    """Dependency-free allow, ask, deny, or defer result."""

    effect: DecisionEffect
    reason: str

    def __init__(self, effect: DecisionEffect, reason: str = "") -> None:
        if effect not in ("allow", "ask", "deny", "defer"):
            raise ValueError(f"invalid kernel decision effect {effect!r}")
        self.effect = effect
        self.reason = reason


def unjudged(reason: str) -> KernelDecision:
    """One machinery bail-out: the kernel cannot judge, so it defers.

    The shell boundary decides what no-judgment means: a sandboxed
    execution runs confined by the OS, an unsandboxed one converts to a
    deny naming the escalation recipe.
    """
    return KernelDecision("defer", reason)
