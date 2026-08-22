<!-- Generated from lup.devtools.harness.content.docs.permissions by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Permission Policy

How the generated hooks decide allow, ask, defer, or deny, and the two
markers that change a decision. The guidance carries the rule; this page
carries the mechanism a denial sends you to.

## Sources of truth

Permissions come from the canonical semantic policies in `lup.policy` and the
application-owned `HookSet` in `devtools/harness/catalog.py`. Harness
generation compiles one hermetic dispatcher and runtime for each native
plugin. Never edit generated dispatcher or runtime files — change the
canonical source and regenerate.

## Shell classification

The policy classifies each shell command against
`lup.policy.vocabulary.default_vocabulary` as
`devtools/harness/content/shell_vocabulary.py` selects it, every URL scope,
and each edit in a batch. `lup.policy.shell_rules` owns the shape that table
takes and its erasure into the rows the kernel reads, never the words; the
project states only where it differs from what the library offers — a
downstream toolchain to add, a command it judges differently, one it drops —
so declaring `lake` costs one entry rather than a copy of every command the
library already judged. The shell
lattice reserves ask for judged risk; unjudged work denies, hinting the
escalation recipe. Under a launcher-verified OS sandbox
(`LUP_SANDBOX_ACTIVE`), unjudged work defers to that boundary, and a
`dangerouslyDisableSandbox` escape re-enters the deny lattice; the sandbox
block derives from the same `HookSet` declaration. A command the sandbox's
`excluded_commands` takes out of isolation re-enters it too, without the
escape: the boundary was told to leave that command alone, so there is
nothing for unjudged work to defer to.

Segments join deny > ask > defer > allow — unjudged rides into a judged
prompt, a judged deny wins the batch. Malformed input fails conservatively.

Where a command runs is a second axis beside that verdict, declared per rule
rather than inferred: `git` states its placement once and every verb beneath
it runs outside the sandbox, because one confined away from its transport or
from the repository's own locks fails however freely it was allowed. An
allow placed outside runs there unprompted; an ask placed outside says so in
the question it asks; a deny short-circuits the axis entirely, and so does a
defer, which hands the sandbox status over with the rest of the decision.
Confinement wins a join, so one segment that must stay inside keeps the whole
line inside. A runtime with no per-call sandbox renders the plain effect.

`uv run <target>` is parsed rather than matched against that table, so its
targets carry the same three answers on a table of their own: each declares
its effect, its placement, and its reason. Blessing is the default and the
common case, and a project that means to stop a target — one that spends
money, runs for an hour, or publishes something — says so there. Leaving it
off is not the same answer: an undeclared target reaches no judgment, which
denies unsandboxed and defers under the boundary, where the policy has
stated nothing and the runtime's own permissions decide.

A target may also carry subcommands, because a toolchain reached through
`uv run` is one target and many commands — a devtools CLI that mostly reads
a repository may have one verb beneath it that opens a paid agent session,
and without this the choice is blessing that verb or refusing the toolchain.
The shape and the walk are the command table's own, so a target with verbs
is judged exactly as the command spelled directly would be, with the
target's own effect as the default beneath them.

Both axes cascade down a table's nesting, and absence means one thing
everywhere: a subcommand or operation omitting `effect` or `sandbox` inherits
the level above it, and one stating either overrides what it inherited in
either direction. `default_effect` is the exception, being required — a
command that never said who decides is a gap a reader sees rather than a
silent allow, and the fallback beneath every table denies.

`$(...)` classifies recursively — the inner command joins the batch and its
opaque result rides only argument-safe commands; command position, deep
nesting, and backticks stay conservative. File writes (redirection, `rm`)
auto-allow only into a repo `tmp/` — the one at the top or any a package
opened beside itself — and the scratchpad (`$TMPDIR`,
`/tmp/claude-*`; reassigning `TMPDIR` asks); discards and fd dups strip;
heredoc-fed writes deny toward Edit/tmp scripts. Loops, conditionals, case
arms, subshells, and brace groups classify recursively over frozen bindings —
literal assignments instantiate, opaque ones (`read`, globs) gate
flag-guarded commands. `find -exec` payloads and `timeout`/`nice` wrappers
recurse, `sed`/`awk` pass read-only screens, quoted-delimiter heredocs are
literal data, and `curl` is read-screened within the declared fetch scopes.

## Fetch scopes

One declared origin table feeds both `WebFetch` and the `curl` screen. A
scope may opt into its subdomains, which also contributes the `*.host`
wildcard to the OS sandbox network allowlist, so both boundaries admit the
same set. Declare any origin an agent should be able to read as a fetch
scope; reserve the sandbox's `extra_domains` for hosts that need egress
without being readable sources. Egress the proxy cannot carry at all — SSH
under a git remote, a daemon socket — is not a scope question: the sandbox's
only lever there is `excluded_commands`, which drops the command out of
isolation rather than widening anything.

## Edit decisions

Edit decisions cover protected paths, marker changes, size, and the canonical
anti-pattern audit. An edit over the size gate alone is deferred — the hook
emits no decision, so auto-accept applies while hard gates stay explicit.

Size is counted in *real* changed lines per change block, and an edit of
three or fewer auto-allows. Imports, comments, whitespace, blank lines,
docstrings, string literals, type annotations, and TypedDict/BaseModel bodies
are not real lines. Pure deletions and single-line `replace_all` renames
auto-allow outright; a multi-line `replace_all` falls through to the size
gate, and a full-file write asks for everything but a package marker — an
`__init__.py` arriving empty or holding nothing but its docstring, where the
question the gate exists to raise has no content to answer it. One carrying
anything else is the module it became, and asks. The anti-pattern audit runs
before any auto-allow, so keeping an edit small cannot outrun it.

Every verdict above is what the kernel reaches when a project says nothing,
and every one of them is nameable. `HookSet.edit_rules` is a `Selection` of
`EditRule`, each naming the axes an edit has — the gate it speaks about, the
file suffixes, the path roles, and whether the change creates, overwrites,
modifies, or deletes — plus the effect it hands that class and the size
threshold it counts by. The two move independently: a rule may widen how much
counts as small for one suffix without restating who decides when it trips.

Overlapping rules resolve **last-match-wins**, the way `.gitignore` reads, so
a project writes the broad statement first and carves its exceptions after
it. A repository whose conventions are Python conventions says so in two
entries — the content gates allow, then `.py`/`.pyi` ask — and leaves prose,
data, and other toolchains to be reviewed in the diff rather than at the
hook. Most-specific-wins was rejected: it makes a table's meaning depend on a
specificity ordering nobody wrote down, and has no answer at all for two
rules of equal reach.

The gate ids include the two that deny removing review feedback. A gate a
project cannot reach is one whose rightness this library asserted on that
project's behalf, and an escape hatch stated in a declaration somebody
reviews is better than the fork it would otherwise take. Moving one gate
moves nothing adjacent: softening `feedback-removed` leaves `claim-removed`
denying, because the two are about different things.

A project that declares an **acceptance guard** adds one gate ahead of all
of those, over every root it gave the `test` role. An ordinary session is
asked before it edits a test, because a test that encodes the wrong
behaviour has to be fixable by someone who can weigh that; a session
declared autonomous is refused, because for it these tests are the
specification it is implementing against, and rewriting a specification to
match an implementation is the failure the guard exists to catch. It answers
before the gates below rather than through them — including pure deletion,
which would otherwise wave through removing the test outright, and the
protected-path rules, whose autonomous release must not survive a refusal
aimed at exactly that caller. This is the one place autonomy costs a caller
more rather than less. Declaring no guard leaves tests judged by the
ordinary lattice, which is right for a project that does not implement
against fixed acceptance tests.

The
resolver's worker receives only its declared autonomous edit exceptions;
temporary paths, human-owned files like `README.md`, marker changes, and
anti-pattern violations retain their guardrails in every mode.

A few of those guardrails open only for a gate a human granted — creating a
devtools module, adding an anti-pattern suppression. What a lease holds is
written in one document per lease, and every judge reads it at the moment it
judges: the canonical policy in the composing process and the deployed
dispatcher in the session's own. The session environment names that document
and never carries its contents, so a gate granted while the session runs
reaches it and one taken back stops applying, with no restart either way. A
narrowed document parks the run rather than silently reducing what a worker
may do. Nothing else grants: a name outside the declared vocabulary is
dropped, and an unreadable document is no grant at all.

Autonomy follows the identity a launcher declares for the session it starts,
carried in the environment and matched against the resolver's own
`worker_identity`, so it reaches a top-level worker session on either runtime
rather than only a natively dispatched subagent. A session that is not
autonomous declares the empty identity rather than staying silent: runtimes
merge a session's environment over the launching process's, so silence would
inherit whatever the operator had exported. A hook script is spawned by the
runtime with the runtime's environment, so an agent exporting the variable
inside a shell tool call never reaches the dispatcher that judges it.

## Two markers change a decision

The guidance spells both; this is what each one does.

- The escalation marker, `lup: escalate: <why>` as the leading comment line
  of a shell command, promotes a classified deny or ask into an approval
  question carrying that reason. It is the recovery path when work is denied
  as unjudged: reshape the command into the allowed vocabulary, or escalate
  with a reason.
- The typed suppression marker, `lup: ignore[<rule-id>]` as a comment on the
  offending line, silences exactly the anti-pattern it names and no other, so
  the site still trips every rule it left unnamed. [contributing.md](contributing.md)
  carries the scoping — where the marker must sit, comma-separated ids, the
  flagged bare form, and the file-wide placement.

Each rule id is shown in the deny message that cites it, and indexed in
[rules.md](rules.md).

## Asking before spending a turn on it

A denial is the ordinary way to learn a verdict, and it costs a turn. Two
commands answer the same question up front, against the declared policy
rather than a reading of this page:

```bash
uv run lup-devtools dev policy '<the command as you would run it>'
uv run lup-devtools dev vocabulary --provenance
```

`dev policy` prints the decision and the sentence explaining it — the same
sentence the hook would have shown — for a shell command, and takes the same
lattice through the same segments, so a pipeline or a `$(...)` answers as it
actually would. `dev vocabulary` prints every shell form the vocabulary
judges and where each rule came from, which is the one to reach for when the
question is "what *would* be allowed here" rather than "is this".

## How one decision reaches two runtimes

The generated plugins enforce permissions without importing lup, yet decide
identically to the library.

1. **Canonical sources** — the `HookSet` in `devtools/harness/catalog.py`
   (protected edit roots, allowed fetch scopes, policy ids, and the shell and
   edit selections), the anti-pattern rule set in `lup.codescan.antipatterns`,
   and the offered shell vocabulary in `lup.policy.vocabulary`. Each selection
   is resolved by `HookSet.resolved_shell_rules` and
   `HookSet.resolved_edit_rules` and nowhere else: a second place that knew
   which defaults a selection layers over is how a session comes to decide
   differently from the plugin its own declaration generated.
2. **Library layer** — `lup.policy.rules` validates those inputs as Pydantic
   surfaces and erases them into primitive rows; `lup.policy.kernel` — the
   hermetic, stdlib-only decision core — interprets those rows to reach every
   shell, fetch, and edit verdict; `lup.policy.chain` composes policies
   deny-before-ask; the adapters' `native` modules decode wire payloads into
   `lup.policy.models` events and render decisions back.
3. **Assembly** — `lup.policy.bundle` reads the kernel source verbatim and
   renders the erased rows as data files; the adapter hook renderers emit
   `hooks/hooks.json`, the dispatcher `hooks/scripts/policy.py`, and
   `hooks/runtime/{kernel.py,policy_data.py}` into each plugin tree.
4. **Equivalence** — the shared fixture suite runs the same cases through the
   library policies and the assembled runtime and requires identical verdicts.

Every rule id a denial cites is indexed in [rules.md](rules.md).
[harness.md](harness.md) covers changing the declarations above, and
[platform-differentiation.md](platform-differentiation.md) records where the
two dispatchers deliberately differ.
