# Apply flow: issue → implement → commit → close

Every port leaves the same trail, so anyone reading this repository sees that upstream bugs are
being tracked and which ones landed here. Everything written to GitHub is in English.

## 1. Open the issue in this repository

```
gh issue create -R <fork> --title "<type>: <what> (upstream #<n>)" --body-file <file>
```

`<type>` is `fix`, `feat` or `docs`. The body is short; the upstream item already holds the
details, so link it instead of repeating it:

```
Upstream: <url of the upstream issue or PR>

**Why it applies here:** one or two sentences — the concept and the fork counterpart.

**What will be done:** one or two sentences — file(s), behaviour, test.
```

For a feature the user approved from an `ask-user` proposal, say so ("approved from the upstream
sync review of <date>").

## 2. Implement

Fix the fork's own code in its own idiom, add the test that fails before and passes after, run the
whole suite (`uv run python -m pytest`) and `ruff check`/`ruff format --check`. Keep the change to
the files the item needs.

## 3. Record the catalog entry

```
node upstream-sync.mjs record --kind <issue|pr> --number <n> --verdict applied \
  --reason "..." --action "..." --fork-issue <fork issue number> [--decided-by user]
```

`action` says what changed and where (files, test) in one or two sentences — this is the line the
console report prints.

## 4. Commit

Stage only the files of this item plus the catalog (`.claude/upstream-sync/entries.jsonl`), never
the whole tree — other items may be in flight in the same working tree:

```
git add -- <files of this item> .claude/upstream-sync/entries.jsonl
git commit -m "<type>(<area>): <summary> (closes #<fork issue>, upstream #<n>)"
```

One line, no trailer, no attribution (repository rule). Then push:

```
git push
```

Record the sha afterwards with a second `record` line carrying `--commit <short sha>` (the entry
is append-only; the latest line wins), or run `record` only once, after the commit, with both
`--fork-issue` and `--commit`.

## 5. Close the issue

The pushed commit closes the issue through `closes #n`; still leave the one-line trail and make
sure it is closed:

```
gh issue comment <fork issue> -R <fork> --body "Done in <short sha>: <one line>."
gh issue close <fork issue> -R <fork> 2>/dev/null || true
```
