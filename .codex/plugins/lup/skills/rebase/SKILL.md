---
name: rebase
description: "Clean up commit history on the feature branch and open/update a PR"
---

# Rebase and PR

Clean up the commit history on the current feature branch, push it, and open (or update) a PR.

## Determine Branches

### Base branch (`<base>`)

Run `uv run lup-devtools dev pr sync-base --json` (step 1 below) -- it reports the base branch and a `base_source`. `recorded` (from worktree creation) and `explicit` are authoritative. `guessed` means topology alone picked it: the command merges nothing and exits non-zero. Ask the user directly, offering concrete options, and wait for the answer: which branch is the true base, then rerun as `sync-base --base <branch>`.

Confirm against the divergence, not the name that sounds right. `git log --oneline <candidate>..HEAD` on the true base shows this branch's own commits; on a wrong one it shows hundreds, which is the tell that the whole history between the two branches is about to be treated as yours.

### PR target (`<target>`)

If a target branch was provided as an argument (`the arguments supplied with this skill invocation`), use it. Otherwise, `<target>` defaults to `<base>`.

## Pre-rebase Validation

### 1. Commit pending changes

Invoke `$lup:commit` to commit any uncommitted work before starting the rebase.

### 2. Sync and merge base

```bash
uv run lup-devtools dev pr sync-base --json
```

A `guessed` base exits non-zero having merged nothing -- settle the base as above and rerun with `--base <branch>`. If conflicts are reported, resolve with `$lup:merge` (no argument) first.

Merging the base commonly leaves a generated tree behind its source -- most often the ownership manifest, which the next `dev check` reports as `harness drift: FAIL` with a stale proof. That is the merge working, not a conflict: run `lup-devtools harness generate all` until it reports `ownership=present`, and commit what it writes.

### 3. Fold local grants into the canonical policy

Check whether `.codex/config.local.toml` exists. Its permission entries are ad-hoc grants one person accepted; leaving them there means the next person re-approves the same prompts.

`.codex/config.toml` is a generated artifact (`harness.project-settings`) -- never hand-merge into it. The next `lup-devtools harness generate all` regenerates it from the policy and drops the edit. Shell, fetch, and edit permissions belong to the canonical semantic policy instead.

Classify each entry:

- **Already covered by the policy** -- drop it from the local file. Most read-only commands are.
- **A genuine gap** -- invoke `$lup:hooks` to add it to the canonical policy, regenerate both native plugins, and commit that as a separate commit.
- **User-specific** (plugin toggles, personal model choice) -- leave it in `.codex/config.local.toml`.

### 4. Run checks

Open `uv run lup-devtools dev check` with `exec_command`, which holds a live PTY, and read what it has emitted with `write_stdin` carrying no keystrokes. Keep the one session for as long as the command runs: re-running it starts over and loses everything it already reported

It runs ruff, pyright, and the test suite, and reports as it goes rather than
only at the end.

Fix any failure this branch introduced. A failure the base already carries is not this branch's to fix: confirm it by running the same check on `<base>`, name it and its origin when reporting, and continue. Fixing it here buries an unrelated change in this PR; staying silent about it lets the next run inherit it as though it were yours.

## Process

### 5. Push and open PR

Open the PR **now, before the history is rebuilt** -- never after. The force-push in step 9 lands in the PR timeline as a force-push event, so the PR carries both the history as it was actually worked and the cleaned sequence that replaced it. Creating it after the rebuild saves one body update and throws that whole trace away.

```bash
uv run lup-devtools dev pr push --json
```

**If no existing PR** (first run), draft a title and summary, then:

```bash
uv run lup-devtools dev pr create --base "<target>" --title "<title>" --body-file "<path>"
```

Write the body to a file and pass `--body-file`. A body worth reading has headings, code spans and prose, and prose has apostrophes: as a `--body` argument every one of them is yours to escape, and a missed one truncates the document into a shell parse error naming an offset rather than the body. `--body` stays for a one-liner.

**If PR already exists**, skip -- we'll force-push the cleaned history later.

### 6. Understand all changes

- Review the full diff: `git diff <base>...HEAD`
- Read changed files to understand the complete set of modifications
- Think about logical units of work (features, refactors, fixes, tests, docs)
- **Ignore existing commit history** -- focus on what makes sense as a clean sequence

### 7. Reset and rebuild commits

Mark where the history was before touching it. The rebuild is the one step
that can lose a file, and a rebuild that dropped one looks exactly like a
rebuild that did not -- the tree is clean either way, and the checks pass
because what is missing is what would have failed them.

```bash
git branch -f rebase-backup HEAD
git reset --soft <base>
```

All changes are now staged. For each logical unit of work:
- Selectively unstage with `git reset HEAD <files>`, then stage and commit relevant pieces
- Or use `git commit` with specific files to build atomic commits
- Order logically: dependencies first, then features, then polish
- Use conventional format: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`

### 8. Prove the rebuild lost nothing

Rebuilt commits are a claim about the same tree, so check it against the mark
before the force-push makes it unrecoverable:

```bash
git diff rebase-backup HEAD --quiet && git branch -D rebase-backup
```

Silent means identical, and the backup is gone because it has served. Any
output is a file the rebuild dropped: find it in `git diff rebase-backup HEAD
--stat`, commit it where it belongs, and check again. Never force-push past
this -- the mark is the only copy of what the branch used to hold.

### 9. Force push and update PR

```bash
uv run lup-devtools dev pr push --force --json
```

Update the PR body with a commit list:

```bash
uv run lup-devtools dev pr update <PR_NUMBER> --body-file "<path>"
```

Return the PR URL to the user.

## Guidelines

- **Never rebase dev/main/master**
- **Confirm before force push**
- **Use --force** (not --force-with-lease) -- after `git reset --soft`, --force-with-lease rejects the diverged ref
- **Never force-push an unverified rebuild**: step 8 is what makes the cleaned history a claim you checked rather than one you made
- **Keep meaningful history**: Don't squash everything into one commit
- **Write good messages**: Future you will thank present you
