---
name: memory-recall
description: "Search and recall relevant memories from past sessions via memsearch. Use when the user's question could benefit from historical context, past decisions, debugging notes, previous conversations, or project knowledge -- especially questions like 'what did I decide about X', 'why did we do Y', or 'have I seen this before'. Also use when you see `[memsearch] Recall available if needed` capability hints injected via SessionStart or UserPromptSubmit. Typical flow: search for 3-5 chunks, expand the most relevant, optionally deep-drill into original transcripts via the anchor format. Skip when the question is purely about current code state (use Read/Grep), ephemeral (today's task only), or the user has explicitly asked to ignore memory."
---

You are performing memory retrieval for memsearch. Search past memories and return the most relevant context to the current conversation.

Memory lives in one place per project: `.memsearch/memory/*.md` markdown journals, indexed into a local SQLite database. The CLI runs from the plugin checkout — always call it as `__INSTALL_DIR__/bin/memsearch`, never as a bare `memsearch`.

## Steps

1. **Search**: Run `__INSTALL_DIR__/bin/memsearch search "<query>" -k 5 --json` to find relevant chunks.
   - Choose a search query that captures the core intent of the user's question.
   - Each result carries `content`, `source`, `heading`, `start_line`, `end_line`, `chunk_id` and `score`.

2. **Evaluate**: Look at the search results. Skip chunks that are clearly irrelevant or too generic.

3. **Expand**: For each relevant result, get the full context:
   - **Primary**: `__INSTALL_DIR__/bin/memsearch expand <chunk_id> --json` returns the full markdown section.
   - **Fallback**: if that fails (sandbox restrictions, unreadable database), read the source directly — the search result carries `source`, `start_line` and `end_line`, so `sed -n '<start>,<end>p' <source>` gives the same text.

4. **Deep drill (optional)**: If an expanded chunk contains a rollout anchor (an HTML comment with session/rollout info) and the original conversation seems critical:
   - Run `__INSTALL_DIR__/bin/memsearch transcript <rollout_path>` to retrieve the original conversation turns, including tool calls.
   - If the command reports an unrecognized format, read the referenced file directly and locate the conversation by the session identifier from the anchor.

5. **Return results**: Output a curated summary of the most relevant memories. Be concise — only include information that is genuinely useful for the user's current question.

## When search returns nothing useful

The index is derived and may be empty or still building. The markdown is the source of truth, so read it directly when search comes up short or when the question is too vague to turn into a query:

- `MDIR="${MEMSEARCH_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.memsearch}"; ls -t "$MDIR/memory/" | head -10` — recent daily logs
- `MDIR="${MEMSEARCH_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.memsearch}"; grep -h "^## " "$MDIR/memory/"*.md | sort -u | tail -40` — session headings across all days
- `MDIR="${MEMSEARCH_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.memsearch}"; cat "$MDIR/memory/<YYYY-MM-DD>.md"` — read a specific day

Once a concrete topic jumps out, go back to `memsearch search` with a specific query.

## Output Format

Organize by relevance. For each memory include:
- The key information (decisions, patterns, solutions, context)
- Source reference (file name, date) for traceability

If nothing relevant is found, simply say "No relevant memories found."
