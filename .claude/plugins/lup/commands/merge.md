---
allowed-tools: Bash(git:*), Read, Grep, Glob, Edit, Write, AskUserQuestion, Skill(lup:commit)
description: Carefully merge changes from another branch, using manual application when conflicts are complex
argument-hint: [branch]
---

# Merge Branch

Carefully merge changes from a source branch into the current branch. Unlike `git merge`, this command can read, understand, and manually apply changes piece-by-piece when a naive merge would produce messy conflicts.

**Arguments provided:** $ARGUMENTS

Parse the first argument as the source branch name. If no branch is provided, ask the user which branch to merge.

## Process

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before starting the merge. This ensures a clean working tree and a restore point if the merge goes wrong.

### 2. Assess the situation

```bash
# Current branch and status
git branch --show-current

# Verify the source branch exists
git branch -a | grep <branch>

# Find the merge base
git merge-base HEAD <branch>
```

### 3. Understand both sides

**Source branch (what's being merged in):**

```bash
# Commits on the source branch since divergence
git log --oneline $(git merge-base HEAD <branch>)..<branch>

# Files changed on the source branch
git diff --stat $(git merge-base HEAD <branch>)..<branch>

# Full diff for understanding intent
git diff $(git merge-base HEAD <branch>)..<branch>
```

**Current branch (what we're merging into):**

```bash
# Commits on current branch since divergence
git log --oneline $(git merge-base HEAD <branch>)..HEAD

# Files changed on current branch
git diff --stat $(git merge-base HEAD <branch>)..HEAD
```

Summarize:
- **Source scope**: What the source branch does (features, fixes, refactors)
- **Current scope**: What the current branch does
- **Overlap**: Files changed on both sides

### 4. Predict conflict severity

```bash
# Dry-run merge to see what would conflict
git merge --no-commit --no-ff <branch> 2>&1 || true

# If conflicts arose, list them
git diff --name-only --diff-filter=U 2>/dev/null

# Abort the trial merge
git merge --abort 2>/dev/null || true
```

Classify the merge into one of:

- **Clean**: No conflicts. Proceed with `git merge`.
- **Light conflicts**: A few files with small, clearly-resolvable conflicts. Proceed with `git merge`, then resolve in-place.
- **Heavy conflicts**: Many files, structural reorganization on both sides, or changes that interleave in ways `git merge` handles poorly. Use **manual application** (step 4b).

Present the assessment to the user via AskUserQuestion:
- Show the conflict prediction
- Recommend a strategy (merge vs manual)
- Let the user choose

### 5a. Standard merge (clean or light conflicts)

```bash
git merge --no-ff <branch>
```

If conflicts arise:
1. For each conflicted file, read the full file
2. Classify each conflict hunk by branch scope (see merge-conflict command's decision tree)
3. Resolve: in-scope changes from current branch take priority; out-of-scope changes from source branch take priority; mixed conflicts get combined where possible
4. Stage resolved files and complete the merge

**Bias toward inclusion** — never silently drop code from either side.

### 5b. Manual application (heavy conflicts)

When the conflict surface is too large or structural, apply changes manually instead of fighting git's merge machinery:

1. **Read each changed file from the source branch:**

   ```bash
   # Get the source version of each changed file
   git show <branch>:<filepath>
   ```

2. **For each file, compare source changes against the current version:**
   - Read the current file
   - Read the source branch version
   - Read the merge-base version (the common ancestor)
   - Identify what the source branch *changed* relative to the base
   - Apply those changes to the current version, adapting to any refactoring or restructuring done on the current branch

3. **Apply changes semantically, not textually:**
   - If the source branch added a function, add it to the current file in the right location
   - If the source branch modified a function that was renamed on the current branch, apply the modification to the renamed version
   - If the source branch changed imports, merge the import lists
   - If both sides restructured, use the current branch's structure and port the source's features into it

4. **For ambiguous cases**, use AskUserQuestion:
   - Show both versions
   - Explain what each side intended
   - Recommend an approach

5. **After all files are applied**, create a merge commit that records the source branch as a parent:

   ```bash
   # Stage all changes
   git add <modified-files>

   # Create a merge commit that records lineage
   git commit -m "$(cat <<'EOF'
   merge: incorporate changes from <branch>

   Manual merge — applied changes semantically to avoid conflict churn.
   EOF
   )"
   ```

### 6. Validate

After the merge (either strategy):

```bash
# Check for leftover conflict markers
grep -rn "^<<<<<<< \|^=======$\|^>>>>>>> " <changed-files> || echo "No conflict markers found"

# Verify the tree is clean
git status --short

# Quick sanity check — does it parse/compile?
# (adapt to the project's tooling)
```

Read each modified file to verify the merge result makes sense — no duplicate functions, no broken imports, no dropped code.

### 7. Deletion audit

Compare the merge result against both parents:

```bash
# What existed in the source branch but not in the result?
git diff <branch> -- <changed-files> --stat

# What existed in HEAD (pre-merge) but not in the result?
git diff HEAD~1 -- <changed-files> --stat
```

If any functions, classes, or significant code blocks were dropped, verify the deletion was intentional.

### 8. Report

Summarize:
- Strategy used (standard merge vs manual application)
- Files merged and how conflicts were resolved
- Any items that need the user's attention

## Guidelines

- **Bias toward inclusion**: Never silently drop code. If both sides have value, keep both.
- **Understand intent before acting**: Read commits, not just diffs. A rename on one side must not swallow an addition on the other.
- **Manual application is the escape hatch**: When `git merge` would produce an unreadable mess, skip it entirely and apply changes by understanding what they do.
- **Always confirm with the user** before choosing a strategy, especially for manual application.
- **Preserve lineage**: Even in manual merges, the commit message should reference the source branch so history is traceable.
