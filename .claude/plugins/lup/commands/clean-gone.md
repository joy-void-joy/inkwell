---
allowed-tools: Bash(uv run lup-devtools:*), AskUserQuestion, Skill(lup:commit)
argument-hint: [branch-name]
description: Review branches/worktrees and clean up merged ones
---

# Clean Merged Branches

Review all local branches and worktrees. Identify branches that are fully merged or have completed PRs. Present the merge graph and ask before deleting.

## Arguments

- **branch-name** (optional): Name of a specific branch to remove. If provided, runs in targeted mode. If omitted, runs a full scan.

Raw arguments: `$ARGUMENTS`

Parse the raw arguments: if non-empty, the first word is the **branch name**. Ignore remaining words.

## Process

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before cleaning up branches.

## Targeted Mode (branch name provided)

1. Run `uv run lup-devtools dev survey --json` to get full branch data.
2. Find the target branch in the survey results. If not found, report and stop.
3. Show the branch's status (containment, PR, unique commits) and confirm deletion via AskUserQuestion.
4. Run `uv run lup-devtools dev delete <branch-name>` (add `--force` only with explicit user approval).

## Full Scan Mode (no argument)

### 2. Collect data

```bash
uv run lup-devtools dev survey --json
```

### 3. Classify each branch

Using the survey JSON, classify each branch:

- **DELETE** -- `contained_in` is non-empty (fully contained in another branch), or PR state is `MERGED`
- **STALE** -- Few `unique_commits` AND low `source_diff_lines` (content superseded by integration branch's continued development). Also check for transitive merges: if branch B is contained in a branch whose PR was merged, B's content reached integration transitively.
- **KEEP** -- Has unique commits not captured elsewhere, or has an open PR
- **CURRENT** -- `is_current` is true (never delete, warn if it qualifies)

### 4. Present the merge graph

Show a table with:
- Branch name, containment info, PR status, unique commits, diff lines
- Proposed action (DELETE/STALE/KEEP) with reason
- For STALE branches, show the transitive path

### 5. Confirm and delete

Use AskUserQuestion to confirm before deleting anything. For each confirmed deletion:

```bash
uv run lup-devtools dev delete <branch-name>
```

If safe delete (`-d`) fails, report to user and ask if `--force` is acceptable.

### 6. Report results

List what was cleaned up.

## Guidelines

- Never force-delete without explicit user approval for that specific branch
- Always confirm before deleting anything
- Skip the current branch -- warn the user instead
- A branch merged into ANY other active branch counts as consumed
- For rebased branches: the original feature branch content may be in integration via a rebase PR, even though `--is-ancestor` returns false