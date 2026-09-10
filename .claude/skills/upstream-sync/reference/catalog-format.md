# Catalog format

Two files in `.claude/upstream-sync/` at the repository root (project state, versioned with the
repository, deliberately outside the skill directory; `MEMSEARCH_UPSTREAM_CATALOG` overrides the
location):

- `state.json` — `upstream` (owner/repo), `fork` (owner/repo of this repository, where issues are
  opened), `baseline_commit`, `baseline_date` (everything closed or merged before it is assumed
  present in the fork), `last_synced_at` (stamped by `finish`; the next `list` includes items
  closed or merged after it).
- `entries.jsonl` — one JSON object per line, append-only. The latest line for a given
  `kind` + `number` is the current verdict.

## Entry

| Field | Meaning |
|---|---|
| `kind` | `issue` or `pr` |
| `number` | upstream number |
| `title` | upstream title at analysis time |
| `url` | upstream URL |
| `upstream_state` | `open`, `closed`, `merged` |
| `upstream_updated_at` | upstream `updatedAt` the verdict was made against — the driver re-lists the item when upstream changes it |
| `analyzed_at` | ISO timestamp |
| `verdict` | one of the values below |
| `reason` | one or two sentences: concept, fork counterpart, why the verdict follows |
| `action` | what was changed in the fork (files, test), or the proposal text for `ask-user`; empty otherwise |
| `fork_issue` | number of the issue opened in this repository for an `applied` item |
| `commit` | short sha of the commit that closed it |
| `related` | other numbers this verdict depends on or covers (stacked PRs, duplicate issues) |
| `decided_by` | `ai` or `user` |

## Verdicts

| Value | Meaning | Re-listed on the next run? |
|---|---|---|
| `applied` | concept ported into the fork through an issue and a commit here | only if upstream updates the item |
| `already-covered` | the fork already behaves correctly; `reason` cites the code or test | only if upstream updates the item |
| `not-applicable` | targets a removed subsystem or another platform | only if upstream updates the item |
| `rejected` | applies, but fixing it would add machinery out of proportion; reason says why | only if upstream updates the item |
| `ask-user` | needs the user's decision; `action` holds the proposal | always, until a user-decided verdict replaces it |
| `deferred` | not decidable yet (no reproduction, upstream undecided) | always |
| `superseded` | judged as part of another entry named in `related` | only if upstream updates the item |

## Re-run semantics

`list` fetches open issues, open PRs, and items closed or merged since `last_synced_at` (or since
`baseline_date` on the first run). An item is pending when it has no entry, when its current entry
is `ask-user` or `deferred` (these are re-listed even if upstream no longer returns the item), or
when `upstream_updated_at` in its entry is older than upstream's. The listing marks the last case
as `updated` so the reader knows a previous verdict exists.
