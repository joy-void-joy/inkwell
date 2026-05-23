---
allowed-tools: Bash(uv run lup-devtools:*), AskUserQuestion, Skill(lup:commit)
description: Check PR review status, merge if approved, and clean up branches
---

# Close PR

Check the review status of the feature branch's PR, merge it if approved, and clean up.

## Process

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before proceeding.

### 2. Get PR status

```bash
uv run lup-devtools dev pr status --json
```

If no PR is found, check if the user passed a PR number as an argument. If still nothing, report the error and stop.

### 3. Evaluate reviews

**If there are unresolved review comments or requested changes:**

1. Display all review comments clearly formatted
2. Show which reviews requested changes vs approved
3. **Stop here.** Tell the user to fix the issues and re-run `/lup:rebase`.

**If all reviews are approved (or no reviews but checks pass):**

1. Show the PR summary
2. Confirm the merge via AskUserQuestion

### 4. Merge the PR

```bash
uv run lup-devtools dev pr merge <PR_NUMBER>
```

### 5. Clean up

Delete the merged branch:

```bash
uv run lup-devtools dev delete <BRANCH_NAME>
```

### 6. Report

Summarize what was done: PR merged (with link), cleanup results.

## Guidelines

- Always show review comments before merging -- never skip review feedback
- The user must confirm before merging via AskUserQuestion