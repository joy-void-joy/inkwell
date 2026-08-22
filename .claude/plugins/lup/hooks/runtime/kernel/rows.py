"""Primitive row shapes the generated data file renders into."""

from typing import Literal, TypedDict

from .decision import DecisionEffect, SandboxPlacement

type PathRuleKind = Literal[
    "exact",
    "subtree",
    "name_prefix",
    "new_subtree",
    "contains_part",
    "new_devtools",
]

type RuleLevel = Literal["root", "command", "subcommand", "operation"]
"""Which nesting level of a shell table a resolved value was declared at.

A row's own level is the deepest name it carries; anything shallower means the
value was inherited. ``root`` is the fallback beneath every table, which a
command declaring its own effect always shadows.
"""


class UrlScopeRow(TypedDict):
    """One erased fetch scope: an origin, the path beneath it, and its reason.

    ``include_subdomains`` widens ``host`` to cover names beneath it, so a
    scope can name a documentation site once instead of every subdomain.
    ``any_port`` widens it the other way, for a host whose port is the
    caller's to choose — a local service started with ``--port`` is the same
    service at every one of them, and a scope pinned to one would put the
    question back the first time somebody moved it.
    """

    scheme: str
    host: str
    port: int | None
    path_prefix: str
    reason: str
    include_subdomains: bool
    any_port: bool


class PathRuleRow(TypedDict):
    """One erased protected-path rule and whether review may bypass it.

    ``allow_autonomous`` releases the rule for an identity that already
    reviews its own edits; every other rule holds regardless of caller.
    """

    kind: PathRuleKind
    value: str
    reason: str
    allow_autonomous: bool


type PathRoleName = Literal["production", "test", "scratch"]

type PathRoleKind = Literal["subtree", "contains_part"]
"""The two directory shapes :func:`root_matches` tells apart.

The narrow pair out of :data:`PathRuleKind`, named separately because that is
the whole of what one function answers for: ``subtree`` anchors a root at the
repository top, while ``contains_part`` matches the directory wherever it
sits, for a tree that is what it is regardless of which package holds it.
"""


class PathRoleRow(TypedDict):
    """One erased declaration of what a repository root is for.

    A role names the purpose a tree serves, which is what decides how much of
    the lattice applies to it. ``test`` code is judged by whether it exercises
    production, not by production's own conventions; ``scratch`` is disposable
    by construction, so the verbs that ask before destroying something have
    nothing to protect there.

    ``root`` is a pattern, which is what says how far the declaration reaches:
    a bare root is anchored at the repository top, and a leading ``**/`` names
    the directory wherever it sits.
    """

    root: str
    role: PathRoleName


class AcceptanceGuardRow(TypedDict):
    """One erased decision to hold a project's acceptance tests still.

    A test states the behaviour production owes; editing one moves the target
    the implementation is aimed at, which is a judgement about what the work
    is rather than about how it was done. The two reasons are separate
    because the two callers are: an ordinary session is asked, since a test
    that genuinely encodes the wrong behaviour has to be changeable by
    someone who can weigh that, while a session implementing *against* these
    tests is refused, because for it the tests are the specification and
    rewriting a specification to match the implementation is the failure the
    guard exists to catch.
    """

    ask_reason: str
    autonomous_reason: str


class AntiPatternRow(TypedDict):
    """One erased anti-pattern rule and the syntactic context it inspects."""

    id: str
    pattern: str
    message: str
    context: str
    matcher: str
    """The AST selector this rule declares, or ``""`` where it declares none.

    Named rather than carried, because a row crossing into the hermetic
    runtime is primitive and a callable is not. The association lives at the
    declaration and travels here, so the gate resolves a rule's selector from
    the row it is already matching on instead of from a second list of ids
    that has to be kept in step with it. Where a row names one and the source
    parses, the selector decides the rule and ``pattern`` is not consulted;
    where the source will not parse, the pattern is all there is and it
    decides alone.
    """
    strength: str
    """"strong" when no directive may silence this rule, "soft" when one may.

    The audit refuses a directive on a strong rule; the hook has to refuse the
    same one, or an edit the hook admits is an edit `dev check` then rejects.
    """
    resolution: str
    """"required" when this rule's verdict turns on a resolved declaration.

    The tree is wider than the defect for these, and what settles the
    difference is what a receiver's declaration resolves to. The kernel never
    resolves anything itself — it is a pure function of its inputs — so the
    answer arrives as an input, from whatever the dispatcher was able to run.
    This flag is what tells the dispatcher an answer is worth paying for, and
    almost no edit trips a rule carrying it. Denying one unresolved states a
    verdict the audit then contradicts, and the two block on opposite states
    with no version of the file passing both. So the gate says what it knows:
    resolved, it decides; unresolved, it asks.
    """


class RefusedToolRow(TypedDict):
    """One erased refusal of a native call, and where to go instead.

    ``specifier`` is ``""`` when the whole tool is refused, and otherwise the
    subject that selects one of its uses — the ``artifact-design`` in
    ``Skill(artifact-design)``. ``reason`` is the whole of what the agent is
    told, so it names the surface to reach for and not only the refusal.
    """

    tool: str
    specifier: str
    reason: str


class RunnerTargetRow(TypedDict):
    """One erased ``uv run <target>`` a project judges, and how.

    ``sandbox`` is the same axis :class:`ShellRuleRow` carries, on the one
    surface a command row cannot reach: ``uv`` is parsed rather than matched,
    so a target's placement has nowhere else to be declared. ``effect`` and
    ``reason`` are there for the same reason — a target a project means to
    refuse has nowhere else to say so, and leaving it off the table is not a
    refusal but an absence of one: the verdict becomes no judgment, which a
    confined session leaves to the runtime's own permissions. For a target
    that spends money or runs for an hour, not refusing is precisely what
    the declaration existed to prevent.
    """

    name: str
    sandbox: SandboxPlacement
    effect: DecisionEffect
    reason: str


class ShellRuleRow(TypedDict):
    """One erased shell-command rule the kernel matches by executable name.

    ``subcommand`` and ``operation`` are ``""`` at the levels a rule does not
    constrain; ``ask_flags`` downgrades an ``allow`` to ``ask`` when one of the
    named flags appears among the command's remaining words. On the
    command-level row of a subcommand-gated command, ``ask_flags`` guard the
    global options before the subcommand and ``value_flags`` name globals that
    consume the following word (``git -C <path>``), so a flag value is never
    read as the subcommand. ``allow_flags`` name a pure read-only form of a
    non-allow row (``ssh-add -l``): the row de-escalates to allow only when
    every remaining word is exactly one of the named flags, so clusters,
    ``=`` values, paths, and unresolved expansions never qualify.
    ``read_verbs`` name action-selecting flags of a command that enforces one
    action at a time (``git config --get``): a non-allow row de-escalates to
    allow when a declared verb appears among words that are all literal and
    free of guarded flags, because the verb pins the invocation to its query
    action regardless of the other words.

    ``sandbox`` says where this command has to run, independently of who
    decides it: a verb that reaches a remote is unusable confined however the
    effect reads, and a verb whose blast radius wants the OS boundary keeps it
    however ordinary the effect reads.

    Both axes arrive already resolved down the nesting, so matching one row is
    the whole answer. ``effect_source`` and ``sandbox_source`` say which level
    supplied each value, which is what a reader needs at a verdict they did not
    expect; neither is consulted in reaching one.
    """

    command: str
    subcommand: str
    operation: str
    effect: DecisionEffect
    effect_source: RuleLevel
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel
    ask_flags: list[str]
    allow_flags: list[str]
    read_verbs: list[str]
    value_flags: list[str]
    reason: str


type EditOperation = Literal["create", "overwrite", "modify", "delete"]
"""What a change does to the file it names.

Carried from the adapter rather than inferred, because the two whole-file
operations are indistinguishable once a preimage is resolved: a ``Write`` over
an existing file and an ``Edit`` that happens to replace every line both
arrive as one whole document replacing another. The native call knows which it
was, so it says.
"""


class EditRuleRow(TypedDict):
    """One erased edit rule: which changes it speaks about, and what it says.

    ``gates``, ``suffixes``, ``roles`` and ``operations`` are the axes a rule
    may constrain, each empty where the rule is silent about it — so matching
    a row is four containment tests and nothing composed. Unlike
    :class:`ShellRuleRow`, which is matched by name and answers alone, these
    rows overlap deliberately and the *last* one matching decides, the way
    `.gitignore` reads: a broad statement first, its exceptions after it.

    ``effect`` is ``""`` where a rule moves only the threshold, and
    ``maximum_added_lines`` is ``None`` where it moves only the verdict. The
    two are separate fields because a rule may state either without the other,
    and a single optional verdict could not say "leave who decides alone, but
    count differently here".
    """

    name: str
    gates: list[str]
    suffixes: list[str]
    roles: list[str]
    operations: list[str]
    effect: str
    maximum_added_lines: int | None
    reason: str
