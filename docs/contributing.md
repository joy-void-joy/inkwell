<!-- Generated from lup.devtools.harness.content.docs.contributing by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Contributing

This page is for a contributor arriving cold. It covers getting a working
checkout, deciding where a change belongs, and what has to be green before it
lands. The component guides — [library.md](library.md),
[template.md](template.md), [harness.md](harness.md) — cover *how* to make a
change once you know where it goes.

## Getting set up

```bash
uv sync                                    # both workspace packages
uv run lup-devtools setup                  # interactive: keys, integrations
uv run lup-devtools dev check              # the local pre-flight bar
```

`uv` is the package manager: use `uv add <package>`, never edit
`pyproject.toml` by hand. Secrets go in `.env.local`, which is gitignored;
`.env` holds template defaults. `uv run lup-devtools --help` is the full
command tree.

To launch the repository as a native agent plugin:

```bash
uv run lup-devtools harness claude          # generate, then launch
uv run lup-devtools harness codex
```

## Where does my change go?

| If you are changing… | It belongs in | And you should read |
| --- | --- | --- |
| Anything another project built on lup would want | `packages/lup/` | [library.md](library.md) |
| Anything only this application needs | `src/lup_template/` | [template.md](template.md) |
| A skill, agent, guidance, permission policy, or a page under `docs/` | the `devtools/harness/content/` of whichever half owns its subject | [harness.md](harness.md) |
| Repeated shell incantations | a new `lup-devtools` command | [template.md](template.md) |
| A one-off computation | `lup-devtools py eval`, or a new command | below |

The placement question between the first two rows is the one that matters, and
it has a single test: *would another project built on lup want this?* If yes,
it goes in the library even if only this application uses it today. The
library never imports the application, so a utility placed wrongly in
`src/lup_template/` is unreachable from `packages/lup/` and will have to move
later.

`tmp/` is scratch: gitignored, so nothing written there reaches a diff, a
reviewer, or a human — which is why it does not execute. One-off work takes
the first of these that fits:

1. To read code rather than run it: `py info`, `py source`, `py search`,
   `py imports`, and the codeintel tools, which resolve names through a
   language server and answer without executing anything.
2. To compute something: `uv run lup-devtools py eval '<expression>'`, which
   auto-imports and needs no file. It evaluates inside the sandbox container
   with this checkout's source mounted read-only, so `lup.*` and the
   application package import exactly as they do here while the expression
   itself reaches the container rather than the checkout.
3. Where no sandbox is available: a new `lup-devtools` command — reviewable,
   because `devtools/` lands in the diff.
4. As a last resort, an inline heredoc behind an escalation marker
   ([permissions.md](permissions.md)).

The argument is reviewability, not power: an agent may already edit
`devtools/` and run it.

Never create a tracking file. A `TODO.md`, backlog, or roadmap parks a
decision where no workflow surfaces it again. Deferred work lives as a
`# lup: defer: <text>` note at the site it concerns — where `dev comments`
lists it in its own parked section and `dev check` keeps it visible until
somebody wakes it. That bare spelling is the
default: nothing evaluates a wake condition mechanically, so a bracketed
`defer[<gate>]: <text>` is for a real, externally-checkable gate ("until the
v2 API ships"), never for restating that this code might change again.

A note is right when the subject is the code: a bug worth remarking on, an
idea for a feature, anything the site it concerns can hold. Work whose
subject is the tooling misbehaving — friction, a command that half-completes,
a classifier reporting a failed probe as fact — has no site to sit at, and
becomes a GitHub issue instead. When whether to defer at all is the open
question, it becomes a question to the user rather than any note.

## Git workflow

Development happens in **worktrees**, not branches switched in place, so
several changes can be in flight at once:

```bash
uv run lup-devtools dev worktree create feat-name
```

The worktree is created as a sibling under `tree/`. Never nest one inside
another checkout. `git checkout -b` would make a branch and switch the
current directory in place; `git worktree add` gives the branch a directory
of its own, which is what keeps several live at once. `worktrees/` and
`refs/` are gitignored, the latter holding symlinks to downstream projects.

Commit early, commit often, and keep commits atomic — if the message needs an
"and", it is two commits. The format is `type(scope): description`:

| Type | Use |
| --- | --- |
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `chore` | Maintenance — dependencies, build config |
| `meta` | Harness content and the trees it generates: guidance, settings, skills, hooks |
| `data` | Generated data and outputs |

A `data` commit of generated outputs may go straight to `dev`; code never
does. Session data under `notes/` is gitignored here, so such commits arise
only in a repository that opted into the commit-loop pattern at init.

Two branches: `dev` is the integration branch feature work merges into, and
`main` is stable and receives only reviewed pull requests from `dev`. Never
commit code directly to `dev`.

/lup:rebase pushes, opens the pull request, and rebuilds history
with `git reset --soft main` and a force-push; re-run it after each round of
review fixes. /lup:close merges the approved one and cleans up. /lup:merge guides conflict
resolution — and during a merge the bias is toward inclusion: audit the result
against both parents and confirm every removed function, parameter, or command
was removed deliberately rather than lost to a conflict side.

Generated artifacts are regenerated, never hand-merged. A digest manifest
(`.lup-ownership.json`) conflicts on every parallel branch because each field
is derived, so `.gitattributes` gives it a driver that keeps one side, and
`lup-devtools dev merge-driver` registers that driver in a clone that has not
run `worktree create`. Reconciling such a file hunk by hunk produces a proof
matching neither tree: take either side, run
`lup-devtools harness generate all`, and let `harness check all` confirm it
settled.

## What has to be green

```bash
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
uv run lup-devtools dev check              # markers, anti-patterns, boundaries
uv run lup-devtools harness check all      # generated-tree drift
uv run lup-devtools dev rules --check      # the generated rule reference
```

[quality-pipeline.md](quality-pipeline.md) explains which of the three
automated layers catches what. The short version: `dev git-hooks install`
refuses a commit whose generated artifacts are behind their source and a
push whose branch fails the gate, the per-push CI workflow runs those same
commands and binds whether or not anyone armed the hooks, and the nightly
lane owns everything that needs a real native CLI.

Two conventions catch most first-time review comments:

- **Every function specifies input and output types**, and `Any`,
  `dict[str, Any]`, and `dict[str, object]` are not among them. Use a
  `TypedDict`, a Pydantic model, or `JsonValue`/`JsonObject` from `lup.types`
  for data whose schema lives elsewhere. `# type: ignore` is forbidden; the
  audited `# lup: ignore[rule-id]` escape hatch exists for genuine boundaries.
- **Errors are never silently swallowed.** No `except: pass`, no
  `contextlib.suppress`. Log with `logger.exception()`, handle it, or re-raise.

[rules.md](rules.md) indexes every executable rule with its matching shape and
the module that enforces it. A denial names its rule id, so you rarely need to
read it first.

### The `# lup: ignore` escape hatch

When `Any` or another anti-pattern is genuinely needed — an untyped library
boundary, MCP — an inline ignore requests user approval rather than silencing
the check on its own authority.

Prefer the typed, pyright-style `# lup: ignore[rule-id]`, comma-separating a
list (`# lup: ignore[dict-get, tuple-shape]`), so a site silences exactly the
rule it needs and still trips the others. The bare `# lup: ignore` stays
valid, but the auditor flags it as untyped to nudge migration. The marker sits
on the line that trips the rule, or stands alone directly above it — one
policy for every rule alike, and nowhere else reaches. Inline is the canonical
placement; the line above is where a reason too long for the column budget
goes, since a comment is the one thing the formatter cannot wrap.

A directive naming a rule that nothing it guards trips is refused rather than
approved — it silences nothing, so the approval would buy an exemption the
auditor already calls spurious. The refusal names the rule that does not fire,
and what the line trips instead where it trips something. Rules another
scanner owns are not judged this way: the edit gate carries the anti-pattern
table alone, and a verdict it cannot reach is not one it refuses over. Nor is
the bare form, which names no rule and so silences every rule there is — the
auditor still reports one that guards nothing, so a bare marker the gate
admits can still be a marker `dev check` refuses.

In a file's opening comment block the marker goes file-wide — a standalone
`# lup: ignore` disables anti-pattern checks for the whole file, and
`# lup: ignore[rule-id]` disables only that rule, the way `# pyright: ignore`
works for files.

## Tests

One standard decides whether a test earns its place: **would it catch a
realistic regression?** A test that asserts a fixture back at itself, or that
pins language behavior rather than library behavior, is deleted rather than
maintained.

Two lanes. `tests/unit/` is deterministic, runs on every push, and pins
adopter-visible behavior: security decisions, byte determinism, wire formats,
state persistence. `tests/integration/` carries the `integration` marker, is
deselected by default, and runs against real installed native CLIs and Docker
on the nightly lane — that is where anything requiring a live boundary
belongs, and nothing in the unit lane may infer it.

The strongest fixtures in the repository are the shared policy cases in
`test_semantic_policy.py`: every case runs against both the canonical policy
objects and the assembled hermetic runtime under `python3 -I -S`, so the
dependency-free generated kernel cannot drift from the library. When you touch
`lup.policy`, that suite is the one to run first.

Behavior that must not regress silently is pinned rather than described:
`test_harness_compilation.py` holds byte-deterministic regeneration and the
live tree drift-clean, `test_rule_reference.py` fails when
[rules.md](rules.md) goes stale, and `test_capability_matrix_docs.py` does the
same for the capability matrix.

## Reviewing a change

A change to canonical harness source arrives with its regenerated artifacts,
and both halves are reviewed together. What to look for:

- A prompt change should be understandable from its content module alone.
- A policy-data change should trace to the `HookSet` or a canonical rule
  object — and `hooks/runtime/kernel.py` should be byte-identical to the
  canonical kernel, with configuration confined to `policy_data.py`.
- Both native trees change when a portable declaration does; only the owning
  tree changes for an adapter-private renderer change.
- `.lup-ownership.json` is generated proof, not hand-authored metadata.
- A conflict is never resolved by deleting an unknown file. Classify its
  ownership or leave the conflict explicit.
- No credentials, plugin trust, installed cache contents, active sessions, or
  local profile configuration are ever committed.

Unresolved `# lup:` review notes stay visible in a full local `dev check`.
They are feedback to act on, not lint to clear: a note comes out when the code
or structure it points at has actually changed. Resolving one is a rewrite,
not a removal: fix what the note points at — or, for a question, answer it
definitively in code, docs, or a recorded user decision — then restate the
marker as `# lup: solved: <the note's original words>`, text unchanged, so
the claim sits beside what it claims to fix and can be checked against what
was asked. A `solved:` claim is retired only by the verify-solved review
pass, through `dev comments --retire`; the edit gate refuses a hand-deletion
or rewording for everyone, agent and human alike. `defer:` notes park work
at the site until deliberately resumed, and `ignore[<rule-id>]` hatches are
not feedback at all — they come out with the violation they cover. /lup:resolve runs that pass;
[resolver.md](resolver.md) describes what it does.

## Native evidence and the release gate

Deterministic fixtures run on every change. A scheduled workflow additionally
runs the full integration marker against installed Claude and Codex binaries,
covering the session-id, pager, dynamic-tool-schema, and blocked-edit
boundaries that only a real CLI can prove. `harness doctor` compares installed
versions against the typed evidence ledger; a newer component warns locally
and fails the nightly strict check, while the live job still runs so drift
cannot suppress the evidence needed to review it.

Beyond the ordinary pull-request checks, cutting a release requires two
consecutive scheduled nightly runs in which:

- the credentials-gated native job **completed successfully** — a skipped job
  is not a green run, and a completed failure stays release-blocking;
- the strict evidence job reported no drift between the installed native
  versions and [native-capabilities.md](native-capabilities.md).

Review the probe output together with the evidence ledger rather than updating
the ledger mechanically.
