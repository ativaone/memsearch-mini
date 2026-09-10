---
name: upstream-sync
description: Case-by-case sync of this simplified fork with the upstream repository (zilliztech/memsearch). Lists upstream issues and pull requests not yet judged, evaluates each one against the fork's reduced scope, ports the concept of a fix (never a raw diff) when it applies — through an issue in this repository, a commit that references it and its closure — asks the user before any new feature, and records every verdict in a persistent catalog so nothing is analyzed twice. Use it whenever the user wants to sync, update or reconcile with the original/upstream repo, review or catch up on upstream issues or PRs, asks whether an upstream bug or PR (a number, a link, a title) is fixed or relevant here, or wants the catalog of past verdicts — even when they do not say "upstream" ("o rep original", "o zilliztech", "issues do original", "pull requests do original", "atualizar com o rep original", "upstream sync"). Not for issues filed against this fork on their own (that is ats-issue-flow).
---

# Upstream sync

The fork keeps one job (Claude Code + Codex memory, SQLite, single machine); upstream keeps growing.
This skill is how the fork benefits from upstream bug reports and fixes without importing its
complexity, and how that work stays visible: every port becomes an issue in this repository, a
commit that references it, and a closed issue. The catalog is the memory of the judgment; the
issue list is the public trail.

## Core rules

- **Concept, not diff.** A fix is ported by understanding the bug and fixing the fork's own code,
  which usually differs from upstream's. `git apply` of an upstream patch is never the answer.
- **Scope wins.** Anything that exists only because of a removed subsystem (other platforms,
  Milvus/collections, maintenance, memory-to-skill, compaction, reranker, watcher, Python API) is
  `not-applicable`. The map is in `reference/triage-rules.md`.
- **Features are questions.** A small feature that would fit gets `ask-user` with a one-paragraph
  proposal; the user decides. A whole subsystem, platform, provider family or new dependency is
  `rejected` on scope. Once the user says yes, the feature goes through the same trail as a fix.
- **Simplicity is a constraint, not a preference.** If a real bug can only be fixed by adding
  machinery, prefer `rejected` with the reason over growing the system.
- **One trail per applied item** — issue, commit, push, closure — exactly as in
  `reference/apply-flow.md`. Commits are limited to the files of that item plus the catalog.
- The catalog is the source of truth for "already analyzed": read it through the driver, never
  guess from memory.

## Procedure

1. `node upstream-sync.mjs status` — baseline, last run, counts.
2. `node upstream-sync.mjs list --json > $TMPDIR/pending.json` — every open item plus items closed or
   merged since the last run, minus entries already settled at the same upstream revision, plus
   unanswered `ask-user`/`deferred` entries. Each PR carries a `scope_hint` computed from the files
   it touches; hints are a triage aid, not a verdict.
3. Judge each item with `reference/triage-rules.md`. For large backlogs, fan the judgment out to
   subagents in batches; keep the decision and every code change with the orchestrator.
4. For each item that will be applied, follow `reference/apply-flow.md`: open the issue here,
   implement with tests, run the full suite, record the catalog entry, commit, close the issue.
5. Record every other verdict with `node upstream-sync.mjs record` (single entry or `--from-file`).
6. `node upstream-sync.mjs report --compact` prints the run's outcome to the console — the user
   reads this, not the catalog: every item judged since the previous run, grouped as applied (issue,
   commit, what changed), waiting for the user's decision (the proposal), and archived (grouped by
   reason). Print it verbatim, then add only what the report cannot know: test results and open
   questions. Drop `--compact` for one line per archived item.
7. `node upstream-sync.mjs finish` stamps the run. The user's answers to `ask-user` items are then
   applied through step 4 and recorded with `--decided-by user`.

## Load when

- Deciding a verdict, mapping a PR's files to the fork, handling stacked PRs, writing the reason
  → `reference/triage-rules.md`
- Opening the fork issue, commit message, closing → `reference/apply-flow.md`
- Catalog fields, verdict values, re-run semantics → `reference/catalog-format.md`

Prerequisites: `gh` authenticated with write access to this repository (issues enabled), `node` ≥ 18.
