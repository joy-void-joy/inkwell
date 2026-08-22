"""Where inkwell's shell vocabulary differs from the one lup offers.

The rule models and the groups in :mod:`lup.policy.vocabulary` are library
mechanism; what is *inkwell's* is the selection below — the one command it
judges differently. Changing a verdict here never means editing lup.

Stated as differences rather than as a table, the way a project states which
anti-patterns it retires: :func:`~lup.policy.vocabulary.default_vocabulary` is
what a selection layers over, so adding one command costs one entry instead of
a copy of every command the library already judged.

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
from lup.policy.vocabulary import git_rule
from lup.selection import Selection

SHELL_RULES: Selection[ShellCommandRule] = Selection[ShellCommandRule](
    overrides=[git_rule(guard_force_push=False, redirect_checkout=True)]
)
"""Where inkwell's shell vocabulary differs from the one lup offers.

One entry, because one entry is the whole of the difference. `git` is declared
again to carry the two arguments above; it replaces the offered rule rather
than sitting beside it, so nothing has to reason about which of two rules
named `git` a walk reaches first.

Everything else — `ls`, `grep`, `gh`, `docker`, the guarded tools, the
redirected verbs — arrives from `default_vocabulary()` and is not restated
here. A table that restated them would have to be re-copied every time the
library judged a new command, and the copy that fell behind would read as a
decision rather than as the oversight it was.
"""
