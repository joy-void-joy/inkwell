---
description: "Inspect and modify the canonical semantic permission policy"
allowed-tools: Read, Edit, AskUserQuestion, Bash
arguments:
  - name: arguments
    description: "Optional arguments supplied with the skill invocation"
    required: false
---

# Hooks: Semantic Permission Policy

Update the canonical policy and regenerate its hermetic native dispatchers.

## User's Request

$ARGUMENTS

## Sources of truth

- `packages/lup/src/lup/policy/` owns semantic fetch, shell, edit, aggregation,
  and native-boundary contracts; `policy/shell_rules.py` owns the shape a shell
  vocabulary takes and its erasure to kernel rows, like the fetch scopes and
  anti-pattern set.
- `packages/lup/src/lup/codescan/` owns the rule families — anti-patterns
  (`antipatterns.py`), boundary/spelling seams (`boundaries.py`), capability
  architecture (`capabilities.py`) — indexed by `registry.py` and rendered
  into `docs/rules.md` by `uv run lup-devtools dev rules`.
- `src/lup_template/devtools/harness/catalog.py` owns application URL scopes,
  protected roots, policy IDs, and other composition inputs; the readable
  shell table it declares is `content/shell_vocabulary.py`.
- `tests/unit/test_semantic_policy.py` is the shared canonical/bundled fixture
  suite.
- `packages/lup/src/lup/devtools/harness/content/docs/permissions.py` renders
  `docs/permissions.md`, which describes the lattice a change here moves. It
  is generated like every other page under `docs/`: edit the source module,
  never the rendered file.

Files beneath .claude/plugins/lup/hooks/ under Claude Code, .codex/plugins/lup/hooks/ under Codex are
generated artifacts. Never edit them as the source of a policy change.

## Workflow

1. Classify the request as a semantic rule, an application policy input, or a
   native decoding/rendering capability.
2. Read the relevant canonical source and its cross-native fixtures.
3. Show the current behavior and propose the smallest semantic change. When
   the request did not already settle the decision, Request explicit user approval before changing policy behavior. Reason: a policy change alters what every later session may do.
4. Edit the canonical source and add fixtures for safe, denied, approval, and
   malformed variants as applicable.
5. Where the change moves what the lattice does, say so in the permissions
   doc source above; a verdict that moved without its page moving leaves the
   page describing a policy nobody runs.
6. Run `uv run pytest -q tests/unit/test_semantic_policy.py`.
7. Run `uv run lup-devtools harness generate all` and
   `uv run lup-devtools harness check all`.

Denial must win over approval across batches and shell segments. Unsupported
native approval effects fail closed. Resolve-editor autonomy may relax only the
declared protected/large edit cases; temporary paths, marker changes, and
anti-pattern violations keep their guardrails.

With no arguments, summarize the canonical URL scopes, shell classifications,
protected edit rules, threshold, resolver-editor exceptions, and each native
boundary's approval behavior, then ask what should change.
