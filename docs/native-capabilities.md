<!-- Generated from lup.devtools.harness.content.docs.native_capabilities by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Native capability evidence

This ledger records the native contracts accepted for Lup 0.2. Runtime
versions are evidence boundaries, not branches in shared orchestration. A
capability not proven here is absent from the portable handle or fails before
input; it is never represented by an unsupported-operation stub.

Evidence was refreshed on 2026-08-05 against Claude Code 2.1.222, Claude Agent
SDK 0.2.89, and Codex CLI/app-server 0.145.0.

| Contract | Version | Evidence | Accepted fact |
|---|---:|---|---|
| Claude plugin package | Claude Code 2.1.222 | `claude plugin validate .claude/plugins/lup` passed; [Claude plugin documentation](https://docs.anthropic.com/en/docs/claude-code/plugins) | The generated manifest, commands, agents, and bundled hooks are loadable. |
| Claude runtime | Claude Agent SDK 0.2.89 | Lazy option construction plus direct SDK block, usage, cost, hook, partial-event, fork, and subagent fixtures in `tests/unit/test_adapter_runtime.py`; [Claude SDK documentation](https://platform.claude.com/docs/en/agent-sdk/overview) | Live partial events, interruption, and latest-turn transcript forking are exposed. Steering is absent. Turn output uses only Lup's MCP `submit_output` tool. **Resume is offered but not honoured**: verified on Claude Code 2.1.223, `claude --session-id <uuid> -p` exits 0 and writes no transcript under `~/.claude/projects`, and `--resume` on the id the run reports answers `No conversation found`. So a session holds one live connection across every turn that does not change its submission schema, and treats a refused resume as losing that turn's context rather than the run. |
| Codex plugin package | Codex CLI 0.145.0 | Generated manifest/marketplace fixtures and cache-digest tests; [Codex plugin structure](https://developers.openai.com/codex/plugins/build#plugin-structure) | Skills, project agents, hooks, marketplace metadata, and installed-cache separation use documented locations. |
| Codex hooks | Codex CLI 0.145.0 | `codex --enable hooks features list` reported hooks stable; hermetic dispatcher fixtures in `tests/unit/test_harness_compilation.py`; [Codex hooks](https://developers.openai.com/codex/hooks) | Plugin hook commands receive `PLUGIN_ROOT`. Non-allow policy decisions fail closed because the command-hook boundary has no portable ask effect. Hook trust is never *generated*, but a worktree-scoped home seeds it from the account. Seeding is confirmed to work and is **not** sufficient: a live `codex exec` in a scoped home whose config carries `lup@...:hooks/hooks.json:pre_tool_use:0:0` as trusted and enabled, against an installed plugin, ran a command the policy denies. The same installed dispatcher fed that command as a `PreToolUse` payload denies it and exits 2, so the gap is that `codex exec` does not reach the hook, not the dispatcher or the seeding. `exec` also reports `approval: never`, which alone means the `PermissionRequest` half can never fire. **Treat a non-interactive Codex session as ungoverned until this is resolved.** |
| Codex blocked edit | Codex CLI 0.145.0 | Scheduled `test_codex_plugin_blocks_a_forbidden_apply_patch` installs the generated plugin in an isolated home and requests an anti-pattern edit through the real CLI | The `apply_patch` call is rejected, the target file remains unchanged, and the native session stays alive to report the rejection. A CLI version drift makes the nightly doctor fail until this observation is repeated. |
| Codex app-server lifecycle | Codex CLI 0.145.0 | Version-generated JSON Schema plus routed-notification fixtures; [Codex app server](https://developers.openai.com/codex/app-server) | `thread/start`, `thread/resume`, `thread/fork`, `turn/start`, `turn/steer`, and `turn/interrupt` exist; live notifications are distinct from completed replay. |
| Codex turn tool binding | Codex CLI 0.145.0 | Version-generated `ThreadStartParams`, `TurnStartParams`, `ThreadResumeParams`, and dynamic-tool call/response schemas | `dynamicTools` exists only on `thread/start`. A typed resume or schema transition that would need a new handler is rejected before input to preserve conversation identity. Native `outputSchema` is not enabled alongside Lup submission. |
| Codex custom agents | Codex CLI 0.145.0 | Generated TOML fixture parsing; [custom-agent documentation](https://developers.openai.com/codex/agent-configuration/subagents) | Portable agents render as project-scoped `.codex/agents/*.toml`, outside the plugin. |
| Codex project guidance | Codex CLI 0.145.0 | Generated root fixture; [AGENTS.md documentation](https://developers.openai.com/codex/agent-configuration/agents-md) | Portable repository guidance renders to root `AGENTS.md`. |

The accepted Codex 0.145.0 schema hashes are:

| Schema | SHA-256 |
|---|---|
| `v2/ThreadStartParams.json` | `b3685411ceb8ad264a1920e8facd66301e5280948ef9c2a6871b95d4c19da639` |
| `v2/TurnStartParams.json` | `f23021c02d28b60fccb6dcaaace9ff676127065f8254537265d6622656860dca` |
| `v2/ThreadResumeParams.json` | `2e1d4b62bc09b46ebc54ef9f84fcdd6ca8d37cabb98dedc34b49761ee764c84d` |
| `DynamicToolCallParams.json` | `e36242b331ca665c74993e55abbea381b1c8a961b29a42579029cff1ad26b20d` |
| `DynamicToolCallResponse.json` | `b5fd10c265be9023f038fe3e4c937a8e215906c8ab31e35d61d28bbc0b755af9` |

Regenerate those schemas with:

```bash
codex app-server generate-json-schema --experimental --out <temporary-directory>
```

Review any digest change together with the typed app-server models, captured
fixtures, capability matrix, and this ledger. Do not update the user's CLI as
part of probing.

## Explicit release gaps

- Codex 0.145.0 cannot pass the persistent typed-schema transition acceptance
  sequence `None -> A -> A -> B -> None` while preserving one thread: the
  native schema offers no dynamic-tool field on `turn/start` or
  `thread/resume`. One-shot typed turns and repeated same-schema turns are
  supported; incompatible transitions fail before input.
- Claude steering is not claimed by the 0.2 adapter; its handle field is
  `None`. Partial events and latest-turn transcript forking are implemented.
- Codex exposes project tool groups, including `run_subagent`, through MCP.
  A subagent spec with a non-empty native tool allowlist is rejected because
  app-server thread configuration cannot prove that per-subagent restriction;
  the restriction is never silently widened.
- Both generated dispatchers map a session's declared identity to edit
  autonomy, taking it from the launcher's environment and, on Claude, from the
  hook payload as well. Deterministic dispatcher fixtures pin both channels
  against the installed hook; live confirmation that a native payload carries
  the agent identity is owed by the nightly lane. The environment channel needs
  no such confirmation — the launcher writes what it declares.
- Live authenticated provider smoke tests remain locally opt-in through the
  integration marker, run on the credentials-gated nightly lane, and are not
  inferred from unit fixtures.
- Why `codex exec` never reaches the hook is not yet separated. Codex records
  trust against a hook's *current hash*, so a seeded entry whose hash no longer
  matches this tree's definition is skipped in silence — indistinguishable,
  from outside, from an `exec` mode that evaluates no hooks at all. Codex
  emitted no startup warning and no hook trace either way.
  `--dangerously-bypass-hook-trust` separates the two in one run: if the
  command is refused under that flag the seeded hash is stale, and if it still
  runs, `exec` does not consult hooks. Until someone runs it, neither cause is
  established and the conservative reading — ungoverned — is the one to hold.
