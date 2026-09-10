#!/usr/bin/env node
// Driver for the upstream-sync skill. Dependency-free; reads upstream through `gh api`.
//
//   node upstream-sync.mjs status
//   node upstream-sync.mjs list [--json] [--only issues|prs] [--all]   (--all ignores the catalog)
//   node upstream-sync.mjs show <issue|pr> <number> [--diff]
//   node upstream-sync.mjs record --kind pr --number 606 --verdict applied --reason "..." [--action "..."]
//                                 [--fork-issue 12] [--commit abc1234] [--related 607,608] [--decided-by user]
//   node upstream-sync.mjs record --from-file verdicts.json      (array of entries, same fields)
//   node upstream-sync.mjs report [--since <iso>] [--compact]     (console report of this run's verdicts; run BEFORE finish;
//                                                                  --compact groups archived items by reason)
//   node upstream-sync.mjs finish                                 (stamps last_synced_at)
//
// Verdicts: applied | already-covered | not-applicable | rejected | ask-user | deferred | superseded

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, appendFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// The catalog is project state, kept outside the skill directory: <repo>/.claude/upstream-sync/.
const CATALOG = process.env.MEMSEARCH_UPSTREAM_CATALOG || join(HERE, "..", "..", "upstream-sync");
const STATE = join(CATALOG, "state.json");
const ENTRIES = join(CATALOG, "entries.jsonl");
const VERDICTS = ["applied", "already-covered", "not-applicable", "rejected", "ask-user", "deferred", "superseded"];
const OPEN_VERDICTS = new Set(["ask-user", "deferred"]);

// Files a PR touches say a lot before anyone reads it. Hints, not verdicts.
const REMOVED = [
  /^plugins\/(opencode|openclaw|dsh|hermes|kimi|zcode|pi)\//, /^plugins\/[^/]+\/(skills\/memory-(config|to-skill)|scripts\/maintenance-runner)/,
  /^src\/memsearch\/(core|compact|reranker|maintenance|skills|index_state|index_report|watcher|io|quality|audit|watcher_registry)\.py$/,
  /^(docs\/|mkdocs\.yml|evaluation\/|assets\/|scripts\/sync-|proposals\/)/, /^\.github\/workflows\/(release|docs|stale)/,
];
const SURVIVING = [
  /^src\/memsearch\/(chunker|transcript|config|scanner|cli|store)\.py$/, /^src\/memsearch\/embeddings\//,
  /^plugins\/(claude-code|codex)\//, /^plugins\/_shared\/prompts\//, /^tests\//, /^pyproject\.toml$/,
  /^README\.md$/, /^CLAUDE\.md$/, /^\.github\/workflows\/test\.yml$/, /^\.pre-commit-config\.yaml$/, /^\.gitignore$/,
];

function gh(args, { json = true } = {}) {
  const out = execFileSync("gh", args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  return json ? JSON.parse(out) : out;
}

function readState() {
  return JSON.parse(readFileSync(STATE, "utf8"));
}

function readEntries() {
  if (!existsSync(ENTRIES)) return new Map();
  const latest = new Map();
  for (const line of readFileSync(ENTRIES, "utf8").split("\n")) {
    if (!line.trim()) continue;
    const e = JSON.parse(line);
    latest.set(`${e.kind}#${e.number}`, e);
  }
  return latest;
}

function scopeHint(files) {
  if (!files || files.length === 0) return "unknown";
  const paths = files.map((f) => f.path ?? f);
  const surviving = paths.filter((p) => SURVIVING.some((re) => re.test(p)));
  const removed = paths.filter((p) => REMOVED.some((re) => re.test(p)));
  if (surviving.length === 0 && removed.length > 0) return "removed-only";
  if (surviving.length === 0) return "docs-or-other";
  if (surviving.every((p) => p.startsWith("tests/"))) return "tests-only";
  return removed.length > 0 ? "mixed" : "surviving";
}

function fetchUpstream(state, { includeClosed = true } = {}) {
  const repo = state.upstream;
  const since = state.last_synced_at || state.baseline_date;
  const common = "number,title,labels,updatedAt,createdAt,author,url,body";
  const issues = gh(["issue", "list", "-R", repo, "--state", "open", "--limit", "1000", "--json", common]);
  const prs = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", "1000", "--json", `${common},files,additions,deletions,isDraft,mergedAt`]);
  let closedIssues = [], mergedPrs = [], closedPrs = [];
  if (includeClosed) {
    closedIssues = gh(["issue", "list", "-R", repo, "--state", "closed", "--search", `closed:>=${since.slice(0, 10)}`, "--limit", "500", "--json", `${common},closedAt`]);
    mergedPrs = gh(["pr", "list", "-R", repo, "--state", "merged", "--search", `merged:>=${since.slice(0, 10)}`, "--limit", "500", "--json", `${common},files,additions,deletions,mergedAt`]);
    closedPrs = gh(["pr", "list", "-R", repo, "--state", "closed", "--search", `closed:>=${since.slice(0, 10)} -is:merged`, "--limit", "500", "--json", `${common},files,additions,deletions,closedAt`]);
  }
  const norm = (kind, state, items) => items.map((i) => ({
    kind, number: i.number, title: i.title, url: i.url, upstream_state: state,
    labels: (i.labels || []).map((l) => l.name), author: i.author?.login ?? "", created_at: i.createdAt,
    upstream_updated_at: i.updatedAt, draft: i.isDraft ?? false,
    files: (i.files || []).map((f) => f.path), additions: i.additions ?? 0, deletions: i.deletions ?? 0,
    scope_hint: kind === "pr" ? scopeHint(i.files) : "issue", body: (i.body || "").slice(0, 4000),
  }));
  return [
    ...norm("issue", "open", issues), ...norm("issue", "closed", closedIssues),
    ...norm("pr", "open", prs), ...norm("pr", "merged", mergedPrs), ...norm("pr", "closed", closedPrs),
  ];
}

function pending(items, entries, { all = false } = {}) {
  const out = [];
  for (const item of items) {
    const prev = entries.get(`${item.kind}#${item.number}`);
    let status = "new";
    if (prev && !all) {
      if (OPEN_VERDICTS.has(prev.verdict)) status = `open:${prev.verdict}`;
      else if (prev.upstream_updated_at < item.upstream_updated_at) status = "updated";
      else continue;
    }
    out.push({ ...item, status, previous_verdict: prev?.verdict ?? null });
  }
  // An unanswered ask-user/deferred verdict stays pending even when upstream no longer lists the
  // item (closed before the last run, so the closed-since window skipped it).
  const seen = new Set(out.map((i) => `${i.kind}#${i.number}`));
  if (!all) {
    for (const [key, prev] of entries) {
      if (!seen.has(key) && OPEN_VERDICTS.has(prev.verdict)) {
        out.push({
          kind: prev.kind, number: prev.number, title: prev.title, url: prev.url, upstream_state: prev.upstream_state,
          labels: [], author: "", created_at: "", upstream_updated_at: prev.upstream_updated_at, draft: false, files: [],
          additions: 0, deletions: 0, scope_hint: prev.kind === "pr" ? "unknown" : "issue", body: prev.action || prev.reason,
          status: `open:${prev.verdict}`, previous_verdict: prev.verdict,
        });
      }
    }
  }
  return out;
}

function cmdStatus() {
  const state = readState();
  const entries = readEntries();
  const counts = {};
  for (const e of entries.values()) counts[e.verdict] = (counts[e.verdict] || 0) + 1;
  console.log(`upstream: ${state.upstream}\nfork: ${state.fork || "(unset)"}\nbaseline: ${state.baseline_commit} (${state.baseline_date})\nlast synced: ${state.last_synced_at || "never"}\nentries: ${entries.size}`);
  for (const [k, v] of Object.entries(counts).sort()) console.log(`  ${k}: ${v}`);
}

function cmdList(flags) {
  const state = readState();
  const items = fetchUpstream(state);
  const only = flags.get("only");
  const list = pending(items, readEntries(), { all: flags.has("all") })
    .filter((i) => !only || (only === "issues" ? i.kind === "issue" : i.kind === "pr"));
  if (flags.has("json")) { console.log(JSON.stringify(list, null, 2)); return; }
  console.log(`${list.length} pending (${list.filter((i) => i.kind === "issue").length} issues, ${list.filter((i) => i.kind === "pr").length} PRs)`);
  for (const i of list) {
    const hint = i.kind === "pr" ? ` [${i.scope_hint}${i.draft ? ", draft" : ""}] ${i.files.length} files` : ` [${i.labels.join(",") || "no label"}]`;
    console.log(`${i.kind}#${i.number} ${i.upstream_state} ${i.status}${hint}: ${i.title}`);
  }
}

function cmdShow(kind, number, flags) {
  const repo = readState().upstream;
  const path = kind === "pr" ? `repos/${repo}/pulls/${number}` : `repos/${repo}/issues/${number}`;
  const item = gh(["api", path]);
  const comments = gh(["api", `repos/${repo}/issues/${number}/comments`, "--paginate"]);
  console.log(`# ${kind}#${number}: ${item.title}\nstate: ${item.state}${item.merged_at ? " (merged)" : ""}  updated: ${item.updated_at}  author: ${item.user?.login}\n${item.html_url}\n\n${item.body || "(no body)"}\n`);
  for (const c of comments) console.log(`--- comment by ${c.user?.login} at ${c.created_at}\n${c.body}\n`);
  if (kind === "pr") {
    const files = gh(["api", `repos/${repo}/pulls/${number}/files`, "--paginate"]);
    console.log(`--- files (${files.length}):\n${files.map((f) => `${f.status} ${f.filename} +${f.additions}/-${f.deletions}`).join("\n")}`);
    if (flags.has("diff")) console.log(`\n--- diff\n${gh(["pr", "diff", String(number), "-R", repo], { json: false })}`);
  }
}

function validate(e) {
  const required = ["kind", "number", "verdict", "reason"];
  for (const k of required) if (e[k] === undefined || e[k] === "") throw new Error(`entry ${JSON.stringify(e)} lacks ${k}`);
  if (!["issue", "pr"].includes(e.kind)) throw new Error(`bad kind ${e.kind}`);
  if (!VERDICTS.includes(e.verdict)) throw new Error(`bad verdict ${e.verdict}; expected one of ${VERDICTS.join(", ")}`);
  return {
    kind: e.kind, number: Number(e.number), title: e.title ?? "", url: e.url ?? "", upstream_state: e.upstream_state ?? "",
    upstream_updated_at: e.upstream_updated_at ?? "", analyzed_at: new Date().toISOString(), verdict: e.verdict,
    reason: e.reason, action: e.action ?? "", fork_issue: e.fork_issue ? Number(e.fork_issue) : null, commit: e.commit ?? "",
    related: (e.related ?? []).map(Number), decided_by: e.decided_by ?? "ai",
  };
}

function cmdRecord(flags) {
  mkdirSync(CATALOG, { recursive: true });
  const previous = readEntries();
  let entries;
  if (flags.has("from-file")) entries = JSON.parse(readFileSync(flags.get("from-file"), "utf8"));
  else {
    // A single-entry record inherits title/url/state from the previous line for the same item, so
    // turning an ask-user into applied does not require retyping them.
    const prev = previous.get(`${flags.get("kind")}#${flags.get("number")}`) || {};
    entries = [{
      kind: flags.get("kind"), number: flags.get("number"), verdict: flags.get("verdict"), reason: flags.get("reason") ?? prev.reason,
      action: flags.get("action") ?? prev.action, related: flags.has("related") ? flags.get("related").split(",").filter(Boolean) : prev.related,
      decided_by: flags.get("decided-by") ?? prev.decided_by, fork_issue: flags.get("fork-issue") ?? prev.fork_issue,
      commit: flags.get("commit") ?? prev.commit, title: flags.get("title") ?? prev.title, url: flags.get("url") ?? prev.url,
      upstream_state: flags.get("upstream-state") ?? prev.upstream_state, upstream_updated_at: flags.get("upstream-updated-at") ?? prev.upstream_updated_at,
    }];
  }
  const lines = entries.map((e) => JSON.stringify(validate(e)));
  appendFileSync(ENTRIES, lines.join("\n") + "\n");
  console.log(`recorded ${lines.length} entr${lines.length === 1 ? "y" : "ies"}`);
}

// Console report of everything judged since the previous run (or since --since).
const REPORT_ORDER = ["applied", "ask-user", "deferred", "already-covered", "rejected", "not-applicable", "superseded"];
const REPORT_LABEL = {
  applied: "Applied in the fork", "ask-user": "Waiting for your decision", deferred: "Deferred (re-listed next run)",
  "already-covered": "Archived: already covered by the fork", rejected: "Archived: rejected on scope or cost",
  "not-applicable": "Archived: not applicable (removed subsystem / other platform)", superseded: "Archived: judged as part of another entry",
};

function cmdReport(flags) {
  const state = readState();
  const since = flags.get("since") || state.last_synced_at || "";
  const groups = new Map(REPORT_ORDER.map((v) => [v, []]));
  for (const e of readEntries().values()) {
    if (since && e.analyzed_at <= since) continue;
    (groups.get(e.verdict) || groups.set(e.verdict, []).get(e.verdict)).push(e);
  }
  const total = [...groups.values()].reduce((n, g) => n + g.length, 0);
  console.log(`Upstream sync report — ${state.upstream} — ${total} items judged${since ? ` since ${since}` : ""}\n`);
  const byNumber = (a, b) => (a.kind === b.kind ? a.number - b.number : a.kind.localeCompare(b.kind));
  const compact = flags.has("compact");
  for (const [verdict, list] of groups) {
    if (list.length === 0) continue;
    console.log(`## ${REPORT_LABEL[verdict] || verdict} (${list.length})`);
    const archived = !["applied", "ask-user", "deferred"].includes(verdict);
    if (compact && archived) {
      // One paragraph per distinct reason: items sharing a reason are the bulk of any first run.
      const byReason = new Map();
      for (const e of list.sort(byNumber)) {
        const key = e.reason.replace(/\s+/g, " ").trim().replace(/\(.*?\)/g, "").slice(0, 80);
        if (!byReason.has(key)) byReason.set(key, { reason: e.reason.replace(/\s+/g, " ").trim(), items: [] });
        byReason.get(key).items.push(e);
      }
      for (const { reason, items } of byReason.values()) {
        const heads = items.map((e) => `${e.kind}#${e.number} ${e.title.length > 48 ? e.title.slice(0, 47) + "…" : e.title}`);
        console.log(`- ${items.length > 1 ? `(${items.length}) ` : ""}${reason}\n    ${heads.join(" · ")}`);
      }
    } else {
      for (const e of list.sort(byNumber)) {
        const what = verdict === "applied" || verdict === "ask-user" ? e.action : e.reason;
        const related = e.related?.length ? ` (see #${e.related.join(", #")})` : "";
        const trail = verdict === "applied" && (e.fork_issue || e.commit)
          ? ` → ${e.fork_issue ? `${state.fork || "fork"}#${e.fork_issue}` : ""}${e.commit ? ` @ ${e.commit}` : ""}` : "";
        console.log(`- ${e.kind}#${e.number} ${e.title}${related}${trail}\n    ${what.replace(/\s+/g, " ").trim()}`);
      }
    }
    console.log("");
  }
}

function cmdFinish() {
  const state = readState();
  state.last_synced_at = new Date().toISOString();
  writeFileSync(STATE, JSON.stringify(state, null, 2) + "\n");
  console.log(`last_synced_at = ${state.last_synced_at}`);
}

function parseFlags(argv) {
  const flags = new Map();
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) continue;
    const key = argv[i].slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) { flags.set(key, next); i++; } else flags.set(key, true);
  }
  return flags;
}

const [cmd, ...rest] = process.argv.slice(2);
const flags = parseFlags(rest);
try {
  if (cmd === "status") cmdStatus();
  else if (cmd === "list") cmdList(flags);
  else if (cmd === "show") cmdShow(rest[0], rest[1], flags);
  else if (cmd === "record") cmdRecord(flags);
  else if (cmd === "report") cmdReport(flags);
  else if (cmd === "finish") cmdFinish();
  else { console.error("usage: upstream-sync.mjs status | list | show <issue|pr> <n> | record | report | finish"); process.exit(2); }
} catch (err) {
  console.error(`upstream-sync: ${err.message}`);
  process.exit(1);
}
