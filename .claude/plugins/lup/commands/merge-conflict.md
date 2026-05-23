---
allowed-tools: Bash(uv run lup-devtools:*, git add:*), Read, Grep, Glob, Edit, Write, AskUserQuestion
description: Resolve merge conflicts after a failed merge attempt
argument-hint: [context about the merge and priorities]
---

# Resolve Merge Conflicts

Resolve all merge conflicts in the working tree after a failed merge or rebase.

**User-provided context:** $ARGUMENTS

## Process

### 1. Assess the situation

```bash
uv run lup-devtools dev conflict-status --json
```

This reports the operation type (merge/rebase/cherry-pick), conflicted files, and commits on both sides.

### 2. Understand what each branch does

From the `ours_commits` and `theirs_commits` in the status output, derive the **branch scope** for each side -- a summary of everything each branch is about. Use the user-provided context above for additional priorities.

### 3. Resolve each conflicted file

For each file in `conflicted_files`:

1. Read the full file to see all conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`)
2. Understand what each side changed by reading surrounding code
3. **Classify each conflict hunk by branch scope** (see Decision Tree below)
4. Resolve and remove all conflict markers
5. Validate: no remaining markers, syntactically correct, no duplicate imports
6. Stage: `git add <file>`

### 4. Deletion audit

After resolving all conflicts but **before completing the merge**:

```bash
uv run lup-devtools dev conflict-audit <conflicted-files> --json
```

Review the audit output. If any files have `warning: true`, check that the removals are intentional. **Fix unjustified deletions before completing.**

### 5. Complete the merge

```bash
uv run lup-devtools dev conflict-complete
```

## Core Principle: Bias Toward Inclusion

**Never silently drop code.** When resolving conflicts, the default is to keep both sides. Deleting a function, feature, or parameter that exists on either side requires explicit justification.

## Resolution Decision Tree

### Step A: Scope classification

Run scope classification to identify which conflicted files this branch touched:

```bash
uv run lup-devtools dev conflicts --json
```

Use this output together with the branch scope summary from step 2 to classify each conflict hunk:

- **In-scope** -- The conflict is in code this branch intentionally changed. **Take ours** (HEAD).
- **Out-of-scope** -- The conflict is in code this branch didn't intentionally modify. **Take theirs** (MERGE_HEAD).
- **Mixed / ambiguous** -- Both sides made intentional changes. Proceed to Step B.

**Direction awareness:** In initialization or sync merges, verify which side has more content -- the richer side is the "authority" side.

### Step B: Resolve mixed/ambiguous conflicts

#### Auto-resolve (no user input needed)

- **Non-overlapping additions** -- Both sides add different content. **Combine both.**
- **Clear superset** -- One side is a strict superset. Take the superset.
- **Whitespace / formatting only** -- Take either side consistently.
- **Identical intent** -- Same change, trivially different wording. Take either.
- **Refactoring vs features** -- One side refactored, the other added features. **Keep both.**

#### Ask the user (use AskUserQuestion)

- **Different approaches** -- Both sides solve the same problem differently.
- **Conflicting deletions vs additions** -- One side removes code the other modifies.
- **Structural reorganization** -- Both sides restructured the same section differently.
- **Ambiguous priority** -- Can't tell which version is better without domain knowledge.

**When asking:** Show the exact conflict as labeled code blocks before the AskUserQuestion call. Explain what each side was trying to do. Offer "combine both" when feasible.

## Guidelines

- **Watch for semantic conflicts** -- Combined code must make sense (renamed variables, etc.)
- **Check adjacent code** -- Nearby non-conflicting code may also need updating
- **Err toward larger** -- When unsure, keep the larger version