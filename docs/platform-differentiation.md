<!-- Generated from lup.devtools.harness.content.docs.platform_differentiation by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Platform differentiation and parity

One portable declaration, two native renderings. `portable_harness()` in
`src/lup_template/devtools/harness/catalog.py` is deliberately singular: the
settled architecture is **portable-declaration-plus-adapter-rendering**, not
per-platform declarations with a shared default. Everything a platform does
differently lives in exactly two places — the adapter renderers
(`packages/lup/src/lup/adapters/claude/harness.py`,
`packages/lup/src/lup/adapters/codex/harness.py`, composed by
`packages/lup/src/lup/adapters/harness.py`) and the per-platform generation
recipes (`claude_generation_recipe` / `codex_generation_recipe` in
`packages/lup/src/lup/devtools/harness/generate.py`). A per-platform declaration
layer was considered and rejected: it would let semantic content fork silently,
whereas the adapter seam forces every difference to be a rendering decision
over the same declarations. `compile_claude` / `compile_codex` enforce that:
`reject_rendered_invocations` refuses native invocation sigils in canonical
text, and `reject_native_prose` refuses any word an adapter would have spelled
— so a difference cannot hide in prose.

That second check writes down no vocabulary of its own. It asks each
`NativeSpellings` what it spells and forbids exactly that
(`packages/lup/src/lup/codescan/portable.py`, rule `portable-content`), which
is why adding a location to `TreeLocation` or `PluginLocation` forbids it in
prose the same moment a runtime learns to spell it.

This document is the map of every intended difference and the parity audit of
every generated artifact family. "Parity" means the same semantic content in
each platform's native format — never byte parity.

## Where each intended difference lives

| Concern | Claude | Codex | Why it differs (all deliberate) |
| --- | --- | --- | --- |
| Skill invocation spelling | `/lup:<skill>` (`ClaudeSpellings.render`) | `$lup:<skill>` (`CodexSpellings.render`) | Native sigils. Canonical content stores `SkillInvocation` parts; only the vocabularies spell them. `SkillPattern` carries the placeholder or wildcard form a prompt uses when it teaches the shape of an invocation instead of issuing one. |
| Prompt compilation | `ClaudeSpellings`: `$ARGUMENTS` for `ArgumentsRef`, the structured-question tool for `AskUser`, a delegation call for `Delegate` | `CodexSpellings`: prose arguments reference, a direct instruction to ask, a custom-agent delegation | One neutral `SpelledPromptRenderer` walks the parts; each runtime supplies a `NativeSpellings` for every native word. A new part adds an abstract method neither runtime can be constructed without answering. |
| Harness locations in prose | `.claude/CLAUDE.md`, `.claude/settings.json`, `.claude/plugins/lup/commands/`, … | `AGENTS.md`, `.codex/config.toml`, `.codex/plugins/lup/skills/`, … | `NativePath` and `PluginPath` name a location semantically. `scope="this_tree"` resolves to the reader's own tree; `scope="every_tree"` renders every runtime's spelling in one identical string, which is how prose teaches both at once. |
| Model choice | `model: opus \| sonnet \| haiku \| inherit` in agent frontmatter | row omitted | Agent declarations carry a portable `ModelTier`. Recorded evidence for Codex custom agents covers TOML parsing only, so no alias is proven to spell a tier in; omitting the row inherits the session model. |
| Runtime documentation | Claude Code and Agent SDK origins | Codex origins | `RuntimeDocs` points a reader at its own runtime's docs; both origins are already in the fetch allowlist (`harness/catalog.py`). |
| Escaping the sandbox for one call | a per-call flag on the launching tool | a per-command override on the shell call, with a justification the user answers | `NativeSpellings.escape_sandbox` returns a `Spelling`, so a runtime with no words an agent could be told to use answers `Unsupported` rather than naming a flag its reader cannot pass — and one that has them says so instead of inheriting a gap nobody re-checked. Both resolver entries ask it, so neither can hardcode an escape and neither can be silently absent. |
| Placing a call from the hook's own verdict | the verdict rewrites the call's arguments | no channel: the verdict is an accept or a decline | Two questions, not one, and Codex answers them differently — `NativeSemantics.escapable` is whether a verdict places a call, `agent_escalates` whether the agent places its own. The `escalable` placement turns on the second and travels as reason text, so it survives where a rendered placement cannot. The `outside` Codex has is compiled ahead of the session as a prefix rule instead. |
| Handing a whole document to a tool | the runtime's own file reader | declined, with the reason | `NativeSpellings.read_document` steers away from text extractors, which return an empty string on a scanned page and read as an empty document. Codex's roster reads a document only by running a command over it, and the one tool that takes a file whole accepts images alone. |
| Skills | `.claude/plugins/lup/commands/<name>.md` (description, allowed-tools, argument declarations) | `.codex/plugins/lup/skills/<name>/SKILL.md` (name + description frontmatter) | Claude plugin commands support tool restriction and argument frontmatter; Codex skills do not — arguments arrive as free text. |
| Agents | `.claude/plugins/lup/agents/<name>.md` (Markdown frontmatter) | `.codex/agents/<name>.toml` (custom-agent TOML) | Native agent formats and locations. |
| Plugin manifest + marketplace | `.claude/plugins/lup/.claude-plugin/plugin.json`, `.claude/plugins/.claude-plugin/marketplace.json` | `.codex/plugins/lup/.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json` | Native manifest schemas and marketplace registries. |
| Repository guidance | `.claude/CLAUDE.md` | `AGENTS.md` at the repository root, plus `.codex/config.toml` (`[features] hooks`) | Same guidance document, rendered to each platform's documented location; Codex additionally needs the hooks feature flag. |
| Hook dispatch | Decodes `Bash\|WebFetch\|Edit\|Write`; dispatcher returns structured allow/ask/deny JSON, or no decision to defer a size-only edit to the client's permission mode; edit preimages are inspected (protected paths, marker counts, size, anti-patterns) | Decodes `Bash\|web_fetch\|apply_patch` on both `PermissionRequest` and `PreToolUse`; a `PermissionRequest` returns a structured allow or, for `ask`, no decision at all so native approval prompts the operator; under `PreToolUse` there is nobody to ask, so `ask` joins `deny` at fail-closed exit code 2; a deferred edit is exit 0; `apply_patch` input is opaque and always asks | Both runtimes reach a real approval prompt, and both run the same semantic kernel (`lup.policy`, identical generated `runtime/kernel.py`). Each rendered matcher is the decoded set above widened by `routed_for` with every tool the composing project's refusal table names, on both runtimes alike — a refusal reaches nothing it was not routed for, so the declaration that states one is what registers it. What differs is edit introspection: Claude sees the preimage and can judge markers, size, and anti-patterns, while Codex sees an opaque patch. |
| Autonomous edit identities | `policy_data.py` grants the resolver's `worker_identity`, bare and plugin-qualified, from the hook payload or the session environment | Same identity, from the session environment only | Both lists are derived from `ResolveSpec.worker_identity`, so neither runtime can ship an empty one by omission. Codex hook payloads carry no agent identity, which is why the environment is the channel that reaches every session on both runtimes. |
| OS sandbox boundary | The `HookSandbox` declaration compiles into the `settings.json` `sandbox` block (bwrap network allowlist, human-owned write denials, credential read denials); the launcher verifies `bwrap`/`socat` before exporting `LUP_SANDBOX_ACTIVE` | The launcher establishes an explicit `--sandbox workspace-write` envelope on the interactive command line and exports the flag only for an envelope it set itself; a caller-supplied sandbox flag keeps the deny lattice active | Codex sandbox config has no per-path write denials or domain allowlist, so its envelope is the declaration's strict subset (network off); the resolver paths carry their own explicit envelopes on both platforms. |
| Resolver entry | `/lup:resolve` instructs `uv run lup-devtools harness resolve --adapter claude` | `$lup:resolve` instructs the same command with `--adapter codex` | Both entries only launch the shared persisted Python resolver, and differ solely in the adapter they name — `ResolverEntry` is deliberately undifferentiated because workflow scripts execute in an isolated VM with no shell, leaving the Claude entry nothing to wrap. The entry contract is the CLI's: optional `--run-id <id>` (resume) and repeatable `--answer <question-id>=<value>`, through which the reserved `integration-assembly` gate is approved like any other question. Both rendered entries document it (pinned by `test_generated_resolver_entries_only_launch_the_shared_python_core`, which also asserts no `Workflow(` wrapper appears). |
| Downstream template guidance | `.claude/plugins/lup/TEMPLATE_CLAUDE.md` from `content/template_claude.py` | `.codex/plugins/lup/TEMPLATE_AGENTS.md` from `content/template_codex.py` | Both flavors compose the portable sections in `content/template_sections.py`; only platform slices (guidance-file names, meta-agent naming, edit-hook vs opaque-patch guidance, LSP vs CLI diagnostics, settings, communication idiom) differ. |
| Launch and trust | Launches the verified local plugin directory with `--plugin-dir`; `CLAUDE_CONFIG_DIR` selects the profile | Seeds a persistent per-worktree home from personal authentication and settings, installs and verifies the plugin there; explicit `--codex-home`/`CODEX_HOME` overrides bypass isolation | Native trust models: Claude trusts the workspace plugin, while Codex requires an installed cache and keeps plugin identity in home-level config. |
| Runtime preflight | `claude` CLI version, plugin support, `plugin validate` | `codex` CLI version and cache digest evidence | Each side probes only its own native capabilities (`harness_runtime.py` in each adapter). |
| Usage display | the OAuth usage endpoint for live windows, plus the local stats cache for per-day and per-model detail | the app-server's own account calls for both the metered windows and the daily token buckets | One display over two readers (`lup.usage`, `usage/reader.py` in each adapter). Each side reports a plan's windows and its days into the same report, so the pacing bars, the daily budget, and the `--json` snapshot are decided once. What differs is what each account publishes: fixed named windows and a per-model split on one side, two self-describing windows and no model breakdown on the other — which is why one draws a model legend and the other has none to draw. |
| Sensitive local-only files | `.claude/settings.local.json` | `.codex/config.local.toml` | Native personal-config locations, excluded from generation. |

## Parity audit of generated artifact families

Every family in `.claude/` vs `.codex/`/`.agents/`, with an explicit decision.

| Family | Claude | Codex | Decision |
| --- | --- | --- | --- |
| Skills (26) | `commands/*.md` | `skills/*/SKILL.md` | Parity — same 26 declarations, native formats. |
| Agents (4) | `plugins/lup/agents/*.md` | `.codex/agents/*.toml` | Parity — same 4 declarations, native formats. |
| Plugin manifest | `.claude-plugin/plugin.json` + marketplace | `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` | Parity — native schemas. |
| Hooks (`hooks.json`, `scripts/policy.py`, `runtime/kernel.py`, `runtime/policy_data.py`, `runtime/evidence.json`) | Structured decisions, edit inspection, autonomous identities | Structured decisions on `PermissionRequest`, fail-closed exit codes under `PreToolUse`, opaque patches, identities from the environment | Parity of the semantic kernel (identical `kernel.py`); dispatcher differences intentional per the table above. |
| Guidance | `.claude/CLAUDE.md` | `AGENTS.md` + `.codex/config.toml` | Parity — one document, native locations. |
| Ownership proof | `.claude/.lup-ownership.json` | `.codex/.lup-ownership.json` | Parity — same mechanism per tree. |
| Template guidance | `TEMPLATE_CLAUDE.md` | `TEMPLATE_AGENTS.md` | Parity — shared portable sections, platform slices per flavor. |
| Resolver entry | skill instructs the CLI directly | skill instructs the CLI directly | Parity — neither tree generates a launcher artifact; the shared `harness resolve --adapter <runtime>` CLI is the entry on both sides. |
| `docs/` | rendered by the Claude recipe | none | Intentional single copy — repository documentation at a neutral location, which neither runtime reads from its own tree. Each page renders identically under both prompt renderers, so a second copy would be a byte duplicate and would additionally give two ownership manifests the same paths to manage. The set is declared once in `content/docs/catalog.py`. |
| `settings.json` | `.claude/settings.json` | none | Intentional — Claude-native project settings (plugin enablement, marketplace, permissions, file suggestion). The Codex counterparts are the generated `.codex/config.toml` plus uncommitted personal `config.local.toml`. |
| `scripts/file_suggest.sh` | `.claude/plugins/lup/scripts/file_suggest.sh` | none | Intentional — wired to Claude's native `fileSuggestion` setting; Codex has no equivalent feature. |
| Codex-only files | none | `.codex/config.toml`, `.agents/plugins/marketplace.json` | Intentional — native Codex requirements with no Claude analogue (Claude's marketplace lives inside `.claude/plugins/`). |

## What portable prose may name

Nothing a runtime spells for itself. A skill that means the guidance file, a
settings location, a plugin directory, a model tier, or the runtime's own
documentation says so through a typed part, and the adapter supplies the word:
`scope="this_tree"` when the sentence instructs the reader to act on their own
tree, `scope="every_tree"` when it describes what the repository holds and both
trees must be named. `reject_native_prose` refuses the rest at compile time, so
this is an invariant rather than a convention.

Two things prose may still name, because they are not platform facts. Tool
grants stay literal: `ToolGrant` (`packages/lup/src/lup/types.py`) deliberately
adopts one tool vocabulary for every runtime, so a skill teaching which grants
to declare names them as the closed type spells them. And the SDK symbols in
the guidance's Type Safety section stay literal too — they are importable names
from the library this template builds on, needed verbatim by a reader on either
runtime.
