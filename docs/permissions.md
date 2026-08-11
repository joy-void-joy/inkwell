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

The policy classifies each shell command against the vocabulary in
`devtools/harness/content/shell_vocabulary.py`, every URL scope, and each edit
in a batch. `lup.policy.shell_rules` owns the shape that table takes and its
erasure into the rows the kernel reads, never the words. The shell
lattice reserves ask for judged risk; unjudged work denies, hinting the
escalation recipe. Under a launcher-verified OS sandbox
(`LUP_SANDBOX_ACTIVE`), unjudged work defers to that boundary, and a
`dangerouslyDisableSandbox` escape re-enters the deny lattice; the sandbox
block derives from the same `HookSet` declaration.

Segments join deny > ask > defer > allow — unjudged rides into a judged
prompt, a judged deny wins the batch. Malformed input fails conservatively.

`$(...)` classifies recursively — the inner command joins the batch and its
opaque result rides only argument-safe commands; command position, deep
nesting, and backticks stay conservative. File writes (redirection, `rm`)
auto-allow only into repo `tmp/` and the scratchpad (`$TMPDIR`,
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
without being readable sources.

## Edit decisions

Edit decisions cover protected paths, marker changes, size, and the canonical
anti-pattern audit. An edit over the size gate alone is deferred — the hook
emits no decision, so auto-accept applies while hard gates stay explicit.

Size is counted in *real* changed lines per change block, and an edit of
three or fewer auto-allows. Imports, comments, whitespace, blank lines,
docstrings, string literals, type annotations, and TypedDict/BaseModel bodies
are not real lines. Pure deletions and single-line `replace_all` renames
auto-allow outright; a multi-line `replace_all` falls through to the size
gate, and a full-file write never auto-allows. The anti-pattern audit runs
before any auto-allow, so keeping an edit small cannot outrun it.

The
resolver's worker receives only its declared autonomous edit exceptions;
temporary paths, human-owned files like `README.md`, marker changes, and
anti-pattern violations retain their guardrails in every mode.

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

## How one decision reaches two runtimes

The generated plugins enforce permissions without importing lup, yet decide
identically to the library.

1. **Canonical sources** — the `HookSet` in `devtools/harness/catalog.py`
   (protected edit roots, allowed fetch scopes, policy ids, shell-rule
   extensions), the anti-pattern rule set in `lup.codescan.antipatterns`, and
   the baseline shell vocabulary in `lup.policy.shell_rules`.
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
