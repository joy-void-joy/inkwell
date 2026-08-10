<!-- Generated from lup.devtools.harness.content.docs.quality_pipeline by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Quality pipeline

Three layers guard the repository. Each runs at a different moment and
catches a class of problem the others cannot.

## Commit time: pre-commit

`.pre-commit-config.yaml` declares one local hook. It runs
`uv run lup-devtools harness generate all` and fails the commit when
generation changes tracked files.

The hook triggers only when the staged commit touches generation inputs or
generated output: the harness devtools and the typed content catalogs on both
sides (`src/lup_template/devtools/harness/`), the `lup` library they compile
(`packages/lup/src/lup/` — the adapters, harness, and policy packages plus
the runtime modules the compiled artifacts embed), or the owned native trees
that reconciliation reads (`.claude/`, `.codex/`, `.agents/`, `AGENTS.md`).
Commits outside those paths run no generation.

Unique catch: a canonical harness edit committed without its regenerated
artifacts, or a hand-edit to an owned artifact, surfaces before the commit
exists instead of minutes later in CI. Formatting, lint, type, and test
problems are deliberately not duplicated at commit time; the per-push gate
below owns them.

## Every pull request and push: quality and harness drift

`.github/workflows/harness-drift.yml` runs on every pull request and on
pushes to `main` and `dev`: `ruff format --check`, `ruff check`, `pyright`,
`pytest`, the anti-pattern rules (`dev check --antipatterns`), the native
seam boundaries (`dev check --boundaries`), and the read-only generated-tree
drift check (`harness check all`).

Unique catch: this is the authoritative gate. It binds whether or not a
contributor installed pre-commit, and it is the only layer that runs the
full static and unit-test bar. It never regenerates or commits.

## Scheduled: native nightly

`.github/workflows/native-nightly.yml` runs on a daily cron and on manual
dispatch. The deterministic `evidence` job installs the real Claude and
Codex CLIs and runs `uv run lup-devtools harness doctor all
--strict-evidence` against the typed evidence ledger. The secrets-gated
`native` job runs the full `pytest -m integration` lane across the installed
CLIs.

Unique catch: breakage observable only through a real native CLI boundary —
installed-version drift against the evidence ledger, and live hook, plugin,
and session behavior — which is too slow and credential-bound for the
per-push gate. Release-evidence rules for this lane are in
[contributing.md](contributing.md).

## What a project built on lup runs in CI

One command:

```yaml
- run: uv run lup-devtools dev check
```

That is deliberately the whole of it. `dev check` is the same bar a checkout
runs locally — format, lint, types, tests, review notes, anti-patterns, seam
boundaries, library placement, generated-tree drift, and the guidance budget —
so a green local run and a green pipeline cannot mean different things.

Nothing is generated into an adopter's `.github/`. A workflow file is the
project's own, and a framework that wrote one would be claiming a schedule,
a runner, and a trigger policy that are not its to choose. This repository
generates its own `quality.yml` because it is *this* project's workflow; an
adopter writes the three lines above wherever its pipeline lives.

## Why the pre-commit hook is path-scoped

ADR-012 in [dev-tooling-decisions.md](dev-tooling-decisions.md) records the
decision: the
per-push CI drift check is the gate that binds, so commit-time regeneration
runs only where it can change the outcome — harness-relevant commits — and
everything the pattern might miss still fails `harness check all` in CI.
