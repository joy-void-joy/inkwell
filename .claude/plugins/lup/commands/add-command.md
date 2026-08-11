---
description: "Create a new slash command in the lup plugin"
allowed-tools: Read, Write, Edit, AskUserQuestion
argument-hint: "[name] [description]"
---

# Add New Command

Declare a new skill in the harness catalog. Every native tree's flavor of it is generated from that one declaration.

## Your Task

Help the user create a new skill. A skill is a `models.Skill` declared in Python; the markdown with YAML frontmatter is what generation renders from it.

**Arguments provided**: $ARGUMENTS

### How to Parse Arguments

The first word is the **command name**. Everything after is the **description** (what the command does).

**Examples:**

- `/lup:add-command review` — name is `review`, description not provided (ask)
- `/lup:add-command review Analyze PR diffs and suggest improvements` — name is `review`, description is "Analyze PR diffs and suggest improvements"

### If No Arguments Provided

If `$ARGUMENTS` is empty, proceed to Phase 1 and ask for everything.

## Phase 1: Gather Requirements

Gather any info **not already provided via arguments**, asking the user one question per open point. Skip anything answered inline.

1. **Command name**: What should the command be called? (e.g., `review`, `test`, `deploy`)
2. **Purpose**: What does this command do?
3. **Arguments**: Does this command accept arguments? If yes, define an `argument-hint` (e.g., `[target]`, `[file] [--verbose]`, `<required-arg>`). Arguments are passed to the command via `$ARGUMENTS`.
4. **Tools needed**: Which tools should be allowed? Every grant is a
   `ToolGrant` from `packages/lup/src/lup/types.py` — read that closed type for
   the spellings. Typical shapes: read-only exploration, the same plus writes
   and edits, those plus scoped shell, and a question grant for a skill that
   has to stop and ask.

## Phase 2: Declare the Skill

Commands are generated artifacts. Write the declaration, not the markdown.

1. Decide which half owns it: a skill that automates work *inside* a project is the library's (`packages/lup/src/lup/devtools/harness/content/skills/`); a skill whose subject is standing a project up or keeping it in step with upstream is this repository's (`src/lup_template/devtools/harness/content/skills/`). Create `<name>.py` there, exporting a `SKILL`:

```python
import lup.harness.models as models

SKILL = models.Skill(
    id="skill.<name>",
    name="<name>",
    description="<one-line description>",
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=[<tools from phase 1>],
    argument_hint="<hint from phase 1, omit if no arguments>",
    prompt=models.PromptDocument(parts=[...]),
)
```

Build `parts` from `models.TextPart(text=r'...')` for the prose, splicing in `models.ArgumentsRef()` wherever the prompt needs the raw arguments and `models.SkillInvocation(plugin="lup", skill="<other>")` wherever it names another skill. Never hardcode a slash-command string — the invocation part renders the right syntax for each harness. Copy the raw-string style from a neighbouring skill module.

2. Register it in the `content/catalog.py` of that same half: import `SKILL as SKILL_<NAME>` alongside its siblings, then add `SKILL_<NAME>` to `LIBRARY_SKILLS` (library) or `PROJECT_SKILLS` (this repository). Both lists are alphabetical, and the project catalog composes them into the `SKILLS` the plugin ships.

3. Regenerate both native plugins:

```bash
uv run lup-devtools harness generate all
```

.claude/plugins/lup/commands/<name>.md under Claude Code, .codex/plugins/lup/skills/<name>/SKILL.md under Codex are written by that step — never by hand.

## Phase 3: Verify

After regenerating:

1. Show the user the declaration module, and confirm the generated artifacts appeared for both harnesses
2. Run `uv run lup-devtools dev check` — the ownership manifest must record the new artifacts
3. Explain how to invoke it: `/lup:<command-name>`
4. Ask if any adjustments are needed

## Template Examples

### Read-only analysis skill:

```python
SKILL = models.Skill(
    id="skill.analyze",
    name="analyze",
    description="Analyze code structure and patterns",
    tools=["Read", "Glob", "Grep"],
    prompt=models.PromptDocument(parts=[...]),
)
```

Its prompt body walks the agent through finding relevant files and searching for patterns with `Bash`, reading the key files, and reporting findings in a structured format.

### Skill with arguments:

```python
SKILL = models.Skill(
    id="skill.test-module",
    name="test-module",
    description="Run tests for a specific module",
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=["Bash", "Read", "Glob", "Grep"],
    argument_hint="[module-name] [--verbose]",
    prompt=models.PromptDocument(parts=[...]),
)
```

Its `parts` open with the prose, splice `models.ArgumentsRef()` in after `**Arguments provided**: `, then resume: parse the first word as the module name and `--verbose` as a flag, ask which module to test when the arguments come through empty, then find the test files, run them, and report.

## Notes

- Skill names should be lowercase with hyphens (e.g., `my-command`), and the module file takes the underscored form
- Keep descriptions under 80 characters
- Include clear steps in the prompt body
- Declare a question grant for interactive skills, and splice `models.AskUser(...)` where one is asked
- Set `argument_hint` on the declaration when the skill accepts arguments — use `[optional]` brackets and `<required>` angles
- Splice `models.ArgumentsRef()` into the prompt parts wherever the body needs `$ARGUMENTS` — never type that token literally
- Always include a fallback that asks the user when arguments are empty
- Regenerate after every change; a hand-edited artifact is reverted the next time generation runs
