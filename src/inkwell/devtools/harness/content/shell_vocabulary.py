"""Inkwell's shell auto-allow vocabulary, composed from library groups.

The rule models and the groups in :mod:`lup.policy.vocabulary` are library
mechanism; what is *inkwell's* is the composition below — which groups it
takes and what it passes them. Changing a verdict here never means editing
lup.

Two judgements differ from the library's offered defaults, and both are
arguments rather than a fork:

``guard_force_push=False`` — the rebase flow republishes a branch with
``--force`` every round, so the force is the ordinary case and guarding it
put an approval question on nearly every push. What removes a remote ref
outright stays guarded, because no second push restores it.

``redirect_checkout=True`` — this repository has settled on ``git switch``
and ``git restore``, so ``checkout`` denies and names them instead of asking.
"""

from lup.policy.shell_rules import ShellCommandRule
from lup.policy.vocabulary import (
    docker_rule,
    gh_rule,
    git_rule,
    guarded_tool_rules,
    judged_ask_rules,
    read_only_rules,
    redirected_rules,
)

SHELL_RULES: list[ShellCommandRule] = [
    *read_only_rules(),
    *judged_ask_rules(),
    *redirected_rules(),
    *guarded_tool_rules(),
    git_rule(guard_force_push=False, redirect_checkout=True),
    gh_rule(),
    docker_rule(),
]
