"""SQLite storage: chunk rows, an FTS5 keyword index, and numpy hybrid search.

The markdown journals are the source of truth; this database is a derived index,
rebuildable at any time.  One database per project — no collections, no server.

``Store.open(path, provider=..., model=..., dimension=...)`` is index mode: the
embedding identity is checked against ``meta`` and written back, and a mismatch
empties the index (``allow_rebuild=True``) or raises :class:`IndexMismatch`.
Plain ``Store.open(path)`` is query mode: it writes no identity and checks none,
so ``search`` / ``expand`` / ``stats`` open the database *before* building an
embedder, reading :attr:`provider` / :attr:`model` / :attr:`dimension` to build
the one it was written with.  Query mode takes no write transaction while the
schema is current, so it never contends with a running indexer.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RRF_K = 60
DEFAULT_MAX_FILE_MB = 8.0
_MAX_PARAMS = 500  # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds
_COLUMNS = "content, source, heading, heading_level, start_line, end_line, chunk_id"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chunks ("
    " id INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL,"
    " heading TEXT NOT NULL DEFAULT '', heading_level INTEGER NOT NULL DEFAULT 0,"
    " start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,"
    " embedding BLOB NOT NULL, content TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS chunks_source_idx ON chunks(source)",
)
# Content-bearing FTS5 table (not external-content, not contentless): it stores the
# CJK-segmented text from fts_text() and deletes without contentless_delete, which
# would need SQLite 3.43.  Its rowid is always chunks.id.
_FTS_DDL = 'CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize="unicode61 remove_diacritics 2")'
_FTS_MISSING = (
    "this SQLite build has no FTS5 module, so keyword search is unavailable. Use a Python whose "
    "sqlite3 was built with FTS5, or set MEMSEARCH_NO_FTS=1 for dense-vector search only."
)
# unicode61 treats CJK ideographs, kana and hangul as token characters, so a whole
# run collapses into one token.  Characters in these ranges are split out instead.
_CJK_RANGES = ((0x2E80, 0x2FFF), (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
               (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF66, 0xFF9F), (0x20000, 0x2FA1F))  # fmt: skip


class StoreError(RuntimeError):
    """The index cannot be used as it stands."""


class IndexMismatch(StoreError):
    """The index was written with a different embedding model or dimension."""


def fts_text(text: str) -> str:
    """Index-side segmentation: every CJK/kana/hangul character becomes one token.

    ``北京的天气很好`` is otherwise one token only an exact match on the whole run
    could find; spaced out it becomes tokens :func:`fts_query` can match.
    """
    return "".join(f" {ch} " if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) else ch for ch in text)


def fts_query(query: str) -> str:
    """Turn arbitrary user input into a safe FTS5 expression.

    Every whitespace-separated term becomes a double-quoted string literal (``"``
    doubled), neutralising the whole FTS5 grammar — ``AND``, ``NEAR``, ``*``, ``^``,
    parentheses, column filters and unbalanced quotes are all just text.  CJK terms
    become phrases (``"北 京"``) matching :func:`fts_text`.  Terms are OR-ed; an empty
    query returns ``""`` and the caller skips the keyword leg.
    """
    terms = [" ".join(fts_text(raw).split()) for raw in query.split()]
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms if term)


@dataclass(frozen=True)
class ChunkRecord:
    """One row to write: chunk metadata plus its embedding."""

    chunk_id: str
    source: str
    heading: str
    heading_level: int
    start_line: int
    end_line: int
    content: str
    embedding: Sequence[float]


@dataclass(frozen=True)
class SearchHit:
    """One result row.  Field order is the documented JSON key order."""

    content: str
    source: str
    heading: str
    heading_level: int
    start_line: int
    end_line: int
    chunk_id: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # dataclass field order == documented key order


@dataclass(frozen=True)
class IndexResult:
    """Outcome of one :func:`index_paths` run."""

    indexed_chunks: int
    total_files: int
    failed_files: tuple[tuple[str, str], ...] = ()
    rebuilt: bool = False


class Store:
    """A single project index.  Not thread-safe: use one instance per thread."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        self.fts_enabled = False
        self.rebuilt = False  # True when opening emptied the index
        self._dimension = 0
        self._writes = 0
        self._cache: tuple[Any, Any, Any] | None = None

    @classmethod
    def open(cls, path: str | Path, *, provider: str = "", model: str = "",
             dimension: int = 0, allow_rebuild: bool = False) -> Store:  # fmt: skip
        """Open (creating if needed) the index at *path*.

        Passing any of *provider* / *model* / *dimension* selects index mode; see
        the module docstring for how the two modes differ.
        """
        resolved = Path(path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        store = cls(sqlite3.connect(str(resolved), timeout=10.0, isolation_level=None), resolved)
        try:
            store._apply_pragmas()
            store._ensure_schema()
            if provider or model or dimension:
                store._check_identity(provider, model, int(dimension), allow_rebuild)
            store._dimension = int(store._meta_get("dimension") or dimension or 0)
        except BaseException:
            store._conn.close()
            raise
        return store

    def close(self) -> None:
        self._cache = None
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _apply_pragmas(self) -> None:
        row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not row or str(row[0]).lower() != "wal":
            self._conn.execute("PRAGMA journal_mode=DELETE")  # some network filesystems refuse WAL
        for pragma in ("busy_timeout=10000", "synchronous=NORMAL", "mmap_size=536870912", "temp_store=MEMORY"):
            self._conn.execute(f"PRAGMA {pragma}")

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction; nested uses join the outer one."""
        conn = self._conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        self._writes += 1

    # -- schema ------------------------------------------------------------------

    def _table_exists(self, name: str) -> bool:
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        return self._conn.execute(sql, (name,)).fetchone() is not None

    def _ensure_schema(self) -> None:
        stored = self._meta_get("schema_version") if self._table_exists("meta") else None
        if stored is not None and stored != str(SCHEMA_VERSION):
            with self._write() as conn:
                for table in ("chunks_fts", "chunks", "meta"):
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.rebuilt, stored = True, None
            sys.stderr.write(f"[memsearch] index schema is obsolete; rebuilding as version {SCHEMA_VERSION}\n")
        if stored is None or not self._table_exists("chunks"):
            with self._write() as conn:
                for ddl in _SCHEMA:
                    conn.execute(ddl)
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        self.fts_enabled = self._resolve_fts()

    def _resolve_fts(self) -> bool:
        """Create the FTS5 table, or probe it — taking no write lock once it exists."""
        if os.environ.get("MEMSEARCH_NO_FTS") == "1":
            return False
        create = not self._table_exists("chunks_fts")
        try:
            if create:
                with self._write() as conn:
                    conn.execute(_FTS_DDL)
                    # Backfill, so a database indexed under MEMSEARCH_NO_FTS=1 does
                    # not come back with a silently half-empty keyword index.
                    rows = conn.execute("SELECT id, content FROM chunks").fetchall()
                    pairs = [(rowid, fts_text(content)) for rowid, content in rows]
                    conn.executemany("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", pairs)
            else:
                self._conn.execute("SELECT rowid FROM chunks_fts LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such module" in str(exc).lower():
                raise StoreError(_FTS_MISSING) from exc
            raise
        return True

    def _check_identity(self, provider: str, model: str, dimension: int, allow_rebuild: bool) -> None:
        stored = (self._meta_get("provider"), self._meta_get("model"), self._meta_get("dimension"))
        wanted = (provider, model, str(dimension))
        if stored == wanted:
            return
        if stored == (None, None, None) or self.count() == 0:
            self._write_identity(wanted)
            return
        detail = (f"index was built with {stored[0]}/{stored[1]} (dimension {stored[2]}), but "
                  f"{provider}/{model} (dimension {dimension}) is configured")  # fmt: skip
        if not allow_rebuild:
            raise IndexMismatch(
                f"{detail}. Run 'memsearch index --force' to rebuild it, or restore the previous "
                f"settings with 'memsearch config set embedding.provider {stored[0]}'."
            )
        self.reset()
        self._write_identity(wanted)
        self.rebuilt = True
        sys.stderr.write(f"[memsearch] {detail}; rebuilding the index\n")

    def _write_identity(self, wanted: tuple[str, str, str]) -> None:
        with self._write():
            for key, value in zip(("provider", "model", "dimension"), wanted, strict=True):
                self._meta_set(key, value)

    # -- metadata ----------------------------------------------------------------

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    @property
    def provider(self) -> str:
        return self._meta_get("provider") or ""

    @property
    def model(self) -> str:
        return self._meta_get("model") or ""

    @property
    def dimension(self) -> int:
        return self._dimension

    def mark_indexed(self, when: float) -> None:
        """Record an indexing run.  Called at the *start*, so a crash halfway
        through still leaves the index looking exactly as fresh as it is."""
        self._meta_set("last_index_at", repr(float(when)))

    def last_index_at(self) -> float | None:
        raw = self._meta_get("last_index_at")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def count(self) -> int:
        return int(self._conn.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def sources(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT DISTINCT source FROM chunks")}

    def ids_for_source(self, source: str) -> dict[str, int]:
        """``{chunk_id: rowid}`` for one source file."""
        return dict(self._conn.execute("SELECT chunk_id, id FROM chunks WHERE source = ?", (source,)))

    # -- writes ------------------------------------------------------------------

    def upsert(self, records: Sequence[ChunkRecord]) -> int:
        """Insert or replace *records* by ``chunk_id``.  Returns the rows written."""
        unique = list({r.chunk_id: r for r in records}.values())
        if not unique:
            return 0
        dim = self._dimension or len(unique[0].embedding)
        bad = next((r for r in unique if len(r.embedding) != dim), None)
        if bad is not None:
            raise IndexMismatch(f"embedding for {bad.chunk_id} has {len(bad.embedding)} values, expected {dim}")
        rows = [(r.chunk_id, r.source, r.heading, r.heading_level, r.start_line, r.end_line,
                 _encode(r.embedding), r.content) for r in unique]  # fmt: skip
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO chunks (chunk_id, source, heading, heading_level, start_line, end_line, embedding,"
                " content) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chunk_id) DO UPDATE SET"
                " source = excluded.source, heading = excluded.heading, heading_level = excluded.heading_level,"
                " start_line = excluded.start_line, end_line = excluded.end_line, embedding = excluded.embedding,"
                " content = excluded.content",
                rows,
            )
            if self.fts_enabled:
                ids = self._ids_for_chunk_ids([r.chunk_id for r in unique])
                pairs = [(ids[r.chunk_id], fts_text(r.content)) for r in unique if r.chunk_id in ids]
                conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(rowid,) for rowid, _ in pairs])
                conn.executemany("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", pairs)
        return len(unique)

    def delete_chunk_ids(self, chunk_ids: Iterable[str]) -> int:
        wanted = list(dict.fromkeys(chunk_ids))
        return self._delete_rowids(list(self._ids_for_chunk_ids(wanted).values())) if wanted else 0

    def delete_source(self, source: str) -> int:
        return self._delete_rowids(
            [r[0] for r in self._conn.execute("SELECT id FROM chunks WHERE source = ?", (source,))]
        )

    def reset(self) -> None:
        """Empty the index, keeping the file and the embedding identity."""
        with self._write() as conn:
            if self.fts_enabled:
                conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM meta WHERE key = 'last_index_at'")

    def _delete_rowids(self, rowids: list[int]) -> int:
        if not rowids:
            return 0
        params = [(rowid,) for rowid in rowids]
        with self._write() as conn:
            if self.fts_enabled:
                conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", params)
            conn.executemany("DELETE FROM chunks WHERE id = ?", params)
        return len(rowids)

    def _ids_for_chunk_ids(self, chunk_ids: Sequence[str]) -> dict[str, int]:
        found: dict[str, int] = {}
        for batch in _batched(list(chunk_ids), _MAX_PARAMS):
            sql = f"SELECT chunk_id, id FROM chunks WHERE chunk_id IN ({','.join('?' * len(batch))})"
            found.update(dict(self._conn.execute(sql, batch)))
        return found

    # -- search ------------------------------------------------------------------

    def get_by_chunk_id(self, chunk_id: str) -> SearchHit | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return SearchHit(*row, score=0.0) if row else None

    def search(self, query_vector: Sequence[float], query_text: str, *, top_k: int = 5) -> list[SearchHit]:
        """Hybrid search: dense cosine similarity fused with BM25 by RRF.

        Normalised so a chunk ranked first by both legs scores exactly ``1.0`` and
        one first by a single leg scores ``0.5``; ties break on the row id.
        """
        if top_k <= 0:
            return []
        depth = max(4 * top_k, 50)
        scores: dict[int, float] = {}
        for leg in (self._dense_ids(query_vector, depth), self._keyword_ids(query_text, depth)):
            for rank, rowid in enumerate(leg, 1):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + rank)
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return self._hydrate(ranked, 2.0 / (RRF_K + 1))

    def _dense_ids(self, query_vector: Sequence[float], depth: int) -> list[int]:
        if query_vector is None or len(query_vector) == 0:
            return []
        import numpy as np

        ids, matrix = self._matrix()
        if matrix.shape[0] == 0:
            return []
        vector = np.asarray(query_vector, dtype="<f4").reshape(-1)
        if vector.shape[0] != matrix.shape[1]:
            raise IndexMismatch(
                f"the query vector has {vector.shape[0]} dimensions but the index stores "
                f"{matrix.shape[1]}. Rebuild it with 'memsearch index --force'."
            )
        norm = float(np.linalg.norm(vector))
        sims = matrix @ (vector / norm if norm else vector)
        depth = min(depth, sims.shape[0])
        top = np.argpartition(-sims, depth - 1)[:depth] if depth < sims.shape[0] else np.arange(sims.shape[0])
        # ids are loaded ordered by id, so the row index doubles as the id tie-break.
        return [int(ids[i]) for i in sorted(top.tolist(), key=lambda i: (-float(sims[i]), i))]

    def _keyword_ids(self, query_text: str, depth: int) -> list[int]:
        if not self.fts_enabled:
            return []
        expression = fts_query(query_text or "")
        if not expression:
            return []
        try:
            sql = "SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?"
            rows = self._conn.execute(sql, (expression, depth)).fetchall()
        except sqlite3.OperationalError:
            return []  # a query FTS5 still refuses degrades to dense-only, never fails
        return [rowid for rowid, _ in sorted(rows, key=lambda row: (row[1], row[0]))]

    def _matrix(self) -> tuple[Any, Any]:
        """``(ids, matrix)`` for every chunk, cached until the database changes.

        ``PRAGMA data_version`` catches other connections' writes; the local write
        counter catches this connection's, which do not bump it.
        """
        import numpy as np

        token = (self._conn.execute("PRAGMA data_version").fetchone()[0], self._writes)
        if self._cache is not None and self._cache[0] == token:
            return self._cache[1], self._cache[2]
        rows = self._conn.execute("SELECT id, embedding FROM chunks ORDER BY id").fetchall()
        dim = self._dimension or (len(rows[0][1]) // 4 if rows else 0)
        for rowid, blob in rows:
            if len(blob) != dim * 4:
                raise IndexMismatch(
                    f"chunk row {rowid} stores a {len(blob) // 4}-dimension embedding but the index "
                    f"declares {dim}. Rebuild it with 'memsearch index --force'."
                )
        ids = np.fromiter((row[0] for row in rows), dtype=np.int64, count=len(rows))
        if rows:
            matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype="<f4").reshape(len(rows), dim)
        else:
            matrix = np.zeros((0, max(dim, 1)), dtype="<f4")
        self._cache = (token, ids, matrix)
        return ids, matrix

    def _hydrate(self, ranked: list[tuple[int, float]], normaliser: float) -> list[SearchHit]:
        rows: dict[int, tuple] = {}
        for batch in _batched([rowid for rowid, _ in ranked], _MAX_PARAMS):
            sql = f"SELECT id, {_COLUMNS} FROM chunks WHERE id IN ({','.join('?' * len(batch))})"
            rows.update({row[0]: row[1:] for row in self._conn.execute(sql, batch)})
        return [SearchHit(*rows[rowid], score=score / normaliser) for rowid, score in ranked if rowid in rows]


async def index_paths(store: Store, embedder: Any, paths: Sequence[str | Path], *, force: bool = False,
                      max_chunk_size: int = 1500, overlap_lines: int = 2) -> IndexResult:  # fmt: skip
    """Index every markdown file under *paths* into *store*.

    Incremental by ``chunk_id``: unchanged chunks are never re-embedded unless
    *force* is set, chunks that left a file are deleted, and vanished files are
    pruned — but only under roots that are directories, so indexing a single file
    never prunes unrelated sources.  A file that fails lands in
    :attr:`IndexResult.failed_files` instead of aborting the run.  *embedder* is
    any object with ``model_name``, ``dimension``, ``batch_size`` and
    ``async embed(list[str]) -> list[list[float]]``.
    """
    from .scanner import scan_paths

    store.mark_indexed(time.time())
    files = scan_paths(list(paths))
    max_bytes = _max_file_bytes()
    indexed, failures, seen = 0, [], set()
    for scanned in files:
        source = str(scanned.path)
        seen.add(source)
        try:
            if scanned.size > max_bytes:
                raise ValueError(f"file is {scanned.size / 1048576:.1f} MB, above the MEMSEARCH_MAX_FILE_MB "
                                 f"limit of {max_bytes / 1048576:g} MB")  # fmt: skip
            indexed += await _index_file(store, embedder, source, force=force,
                                         max_chunk_size=max_chunk_size, overlap_lines=overlap_lines)  # fmt: skip
        except Exception as exc:  # one bad file must never stop the run
            failures.append((source, f"{type(exc).__name__}: {exc}"[:2000]))
    roots = [root for root in (Path(p).expanduser().resolve() for p in paths) if root.is_dir()]
    for source in sorted(store.sources()) if roots else ():
        if source not in seen and any(Path(source).expanduser().resolve().is_relative_to(r) for r in roots):
            store.delete_source(source)
    return IndexResult(indexed, len(files), tuple(failures), store.rebuilt)


async def _index_file(store: Store, embedder: Any, source: str, *, force: bool,
                      max_chunk_size: int, overlap_lines: int) -> int:  # fmt: skip
    from .chunker import chunk_markdown, clean_content_for_embedding, compute_chunk_id
    from .scanner import read_utf8_text_replace

    text = read_utf8_text_replace(source)
    chunks = chunk_markdown(text, source=source, max_chunk_size=max_chunk_size, overlap_lines=overlap_lines)
    pairs = [(c, compute_chunk_id(c.source, c.start_line, c.end_line, c.content_hash)) for c in chunks]
    existing = store.ids_for_source(source)
    current = {chunk_id for _, chunk_id in pairs}
    stale = [chunk_id for chunk_id in existing if chunk_id not in current]
    if stale:
        store.delete_chunk_ids(stale)
    if not force:
        pairs = [pair for pair in pairs if pair[1] not in existing]
    if not pairs:
        return 0
    batch_size = embedder.batch_size if embedder.batch_size > 0 else len(pairs)
    total = 0
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        vectors = await embedder.embed([clean_content_for_embedding(chunk.content) for chunk, _ in batch])
        total += store.upsert([_record(chunk, chunk_id, vector)
                               for (chunk, chunk_id), vector in zip(batch, vectors, strict=True)])  # fmt: skip
    return total


def _record(chunk: Any, chunk_id: str, embedding: Sequence[float]) -> ChunkRecord:
    return ChunkRecord(chunk_id, chunk.source, chunk.heading, chunk.heading_level, chunk.start_line,
                       chunk.end_line, chunk.content, embedding)  # fmt: skip


def _encode(vector: Sequence[float]) -> bytes:
    """L2-normalise and pack as little-endian float32, so a dot product is cosine."""
    import numpy as np

    values = np.asarray(vector, dtype="<f4").reshape(-1)
    norm = float(np.linalg.norm(values))
    return np.asarray(values / norm if norm else values, dtype="<f4").tobytes()


def _batched(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _max_file_bytes() -> int:
    raw = os.environ.get("MEMSEARCH_MAX_FILE_MB", "").strip()
    try:
        return int((float(raw) if raw else DEFAULT_MAX_FILE_MB) * 1024 * 1024)
    except ValueError:
        return int(DEFAULT_MAX_FILE_MB * 1024 * 1024)
