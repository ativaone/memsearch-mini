---
name: ats-upstream-sync
description: Case-by-case sync of this simplified fork with the upstream repository (zilliztech/memsearch). Lists upstream issues and pull requests not yet judged, evaluates each one against the fork's reduced scope, ports the concept of a fix (never a raw diff) when it applies, asks the user before any new feature, and records every verdict in a persistent catalog so nothing is analyzed twice. Use it whenever the user wants to sync, update or reconcile with the original/upstream repo, review or catch up on upstream issues or PRs, asks whether an upstream bug or PR (a number, a link, a title) is fixed or relevant here, or wants the catalog of past verdicts — even when they do not say "upstream" ("o rep original", "o zilliztech", "issues do original", "pull requests do original", "atualizar com o rep original", "upstream sync"). Not for issues filed against this fork itself (that is ats-issue-flow).
---

# Upstream sync

The fork keeps one job (Claude Code + Codex memory, SQLite, single machine); upstream keeps growing.
This skill is how the fork benefits from upstream bug reports and fixes without importing its
complexity. The catalog is the memory of that judgment: every issue and PR gets exactly one verdict
per upstream revision, and a new run starts from what is still unjudged.

## Core rules

- **Concept, not diff.** A fix is ported by understanding the bug and fixing the fork's own code,
  which usually differs from upstream's. `git apply` of an upstream patch is never the answer.
- **Scope wins.** Anything that exists only because of a removed subsystem (other platforms,
  Milvus/collections, maintenance, memory-to-skill, compaction, reranker, watcher, Python API) is
  `not-applicable`. The map is in `reference/triage-rules.md`.
- **Features are questions.** A new capability, a config knob that does not exist here, or any
  change that adds complexity to solve something the fork does not suffer from gets the verdict
  `ask-user` with a one-paragraph proposal. Only the user turns that into `applied`.
- **Simplicity is a constraint, not a preference.** If a real bug can only be fixed by adding
  machinery, prefer `rejected` with the reason over growing the system.
- **Never commit.** Changes stay in the working tree for the user's review; the catalog entry
  records what was changed and where.
- The catalog is the source of truth for "already analyzed": read it through the driver, never
  guess from memory.

## Procedure

1. `node upstream-sync.mjs status` — baseline, last run, counts.
2. `node upstream-sync.mjs list --json > $TMPDIR/pending.json` — every open item plus items closed or
   merged since the last run, minus entries already settled at the same upstream revision. Each item
   carries a `scope_hint` computed from the files a PR touches; hints are a triage aid, not a verdict.
3. Judge each item with `reference/triage-rules.md`. For large backlogs, fan the judgment out to
   subagents in batches; keep the decision and every code change with the orchestrator.
4. Port what applies, with tests, and run the full suite (`uv run python -m pytest`).
5. `node upstream-sync.mjs record --from-file <verdicts.json>` (or one `--kind/--number/--verdict`
   at a time).
6. `node upstream-sync.mjs report --compact` prints the run's outcome to the console — the user
   reads this, not the catalog: every item judged since the previous run, grouped as applied (what
   was changed, in a line or two), waiting for the user's decision (the proposal), and archived
   (already covered, rejected, not applicable, superseded — grouped by reason). Print it verbatim,
   then add only what the report cannot know: files changed in the working tree, test results, open
   questions. Drop `--compact` when the user wants one line per archived item.
7. `node upstream-sync.mjs finish` stamps the run. The user's answers to `ask-user` items are
   recorded later with `--decided-by user`.

## Load when

- Deciding a verdict, mapping a PR's files to the fork, handling stacked PRs, writing the reason
  → `reference/triage-rules.md`
- Catalog fields, verdict values, re-run semantics → `reference/catalog-format.md`

Prerequisites: `gh` authenticated (the driver reads upstream through `gh api`), `node` ≥ 18.
