---
allowed-tools: Bash(uv run lup-devtools:*, git:*), Read, Glob, Grep, AskUserQuestion, Skill(lup:commit)
argument-hint: [target-branch]
description: Clean up commit history on the feature branch and open/update a PR
---

# Rebase and PR

Clean up the commit history on the current feature branch, push it, and open (or update) a PR.

## Determine Branches

### Base branch (`<base>`)

Run `uv run lup-devtools dev pr sync-base --json` (step 1 below) -- it auto-detects the base branch. If ambiguous, use AskUserQuestion to ask which branch to use.

### PR target (`<target>`)

If a target branch was provided as an argument (`$ARGUMENTS`), use it. Otherwise, `<target>` defaults to `<base>`.

## Pre-rebase Validation

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before starting the rebase.

### 2. Sync and merge base

```bash
uv run lup-devtools dev pr sync-base --json
```

If conflicts are reported, resolve with `/lup:merge-conflict` first.

### 3. Merge local settings into shared config

Check if `.claude/settings.local.json` exists. If so, merge all sensible settings into `.claude/settings.json` -- permissions, auto-accept patterns, etc. Skip user-specific items. Commit as a separate commit.

### 4. Run checks

```bash
uv run lup-devtools dev pr checks --json
```

Fix any failures before proceeding.

### 5. Review PLAN.md

If `PLAN.md` exists, verify:
- All items completed (`[x]`) or explicitly deferred
- No items in-progress (`[~]`)
- Plan reflects what was built

If incomplete items exist, confirm via AskUserQuestion.

## Process

### 6. Push and open PR

```bash
uv run lup-devtools dev pr push --json
```

**If no existing PR** (first run), draft a title and summary, then:

```bash
uv run lup-devtools dev pr create --base "<target>" --title "<title>" --body "<body>"
```

**If PR already exists**, skip -- we'll force-push the cleaned history later.

### 7. Understand all changes

- Review the full diff: `git diff <base>...HEAD`
- Read changed files to understand the complete set of modifications
- Think about logical units of work (features, refactors, fixes, tests, docs)
- **Ignore existing commit history** -- focus on what makes sense as a clean sequence

### 8. Reset and rebuild commits

```bash
git reset --soft <base>
```

All changes are now staged. For each logical unit of work:
- Selectively unstage with `git reset HEAD <files>`, then stage and commit relevant pieces
- Or use `git commit` with specific files to build atomic commits
- Order logically: dependencies first, then features, then polish
- Use conventional format: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`

### 9. Force push and update PR

```bash
uv run lup-devtools dev pr push --force --json
```

Update the PR body with a commit list:

```bash
uv run lup-devtools dev pr update <PR_NUMBER> --body "<updated body>"
```

Return the PR URL to the user.

## Guidelines

- **Never rebase dev/main/master**
- **Confirm before force push**
- **Use --force** (not --force-with-lease) -- after `git reset --soft`, --force-with-lease rejects the diverged ref
- **Keep meaningful history**: Don't squash everything into one commit
- **Write good messages**: Future you will thank present you