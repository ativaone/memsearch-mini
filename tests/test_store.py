"""Tests for the SQLite store: schema, hybrid search, and incremental indexing."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from memsearch_mini.store import (
    RRF_K,
    SCHEMA_VERSION,
    ChunkRecord,
    IndexMismatch,
    Store,
    StoreError,
    fts_query,
    fts_text,
    index_paths,
)

DIM = 4
E0 = [1.0, 0.0, 0.0, 0.0]
E1 = [0.0, 1.0, 0.0, 0.0]
E2 = [0.0, 0.0, 1.0, 0.0]


def _rec(chunk_id: str, vector: list[float], *, source: str = "/notes/a.md", content: str = "alpha beta",
         heading: str = "Heading", level: int = 2, start: int = 1, end: int = 4) -> ChunkRecord:  # fmt: skip
    return ChunkRecord(chunk_id, source, heading, level, start, end, content, vector)


def _open(path, **kwargs) -> Store:
    kwargs.setdefault("provider", "fake")
    kwargs.setdefault("model", "fake-embed")
    kwargs.setdefault("dimension", DIM)
    return Store.open(path, **kwargs)


@pytest.fixture
def store(tmp_path):
    with _open(tmp_path / "index.db", allow_rebuild=True) as opened:
        yield opened


def _fts_rowids(store: Store) -> list[int]:
    return [row[0] for row in store._conn.execute("SELECT rowid FROM chunks_fts ORDER BY rowid")]


def _chunk_rowids(store: Store) -> list[int]:
    return [row[0] for row in store._conn.execute("SELECT id FROM chunks ORDER BY id")]


# -- schema and metadata ---------------------------------------------------------


def test_open_creates_the_database_and_the_schema(tmp_path):
    path = tmp_path / "nested" / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        tables = {row[0] for row in opened._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meta", "chunks", "chunks_fts"} <= tables
        assert opened._meta_get("schema_version") == str(SCHEMA_VERSION)
        assert opened.fts_enabled is True
        assert opened.count() == 0
        assert opened.sources() == set()
        assert opened.last_index_at() is None
        assert opened.search(E0, "alpha") == []
    assert path.is_file()


def test_open_uses_wal_and_a_busy_timeout(store):
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_index_mode_records_the_embedding_identity(tmp_path):
    with _open(tmp_path / "index.db", allow_rebuild=True) as opened:
        assert (opened.provider, opened.model, opened.dimension) == ("fake", "fake-embed", DIM)
        assert opened.rebuilt is False


def test_query_mode_reads_the_identity_without_writing_meta(tmp_path):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0)])
        before = dict(opened._conn.execute("SELECT key, value FROM meta"))

    with Store.open(path) as reader:
        assert (reader.provider, reader.model, reader.dimension) == ("fake", "fake-embed", DIM)
        assert dict(reader._conn.execute("SELECT key, value FROM meta")) == before
        assert [hit.chunk_id for hit in reader.search(E0, "alpha")] == ["c1"]


def test_query_mode_on_a_fresh_database_writes_no_identity(tmp_path):
    with Store.open(tmp_path / "fresh.db") as reader:
        assert dict(reader._conn.execute("SELECT key, value FROM meta")) == {"schema_version": str(SCHEMA_VERSION)}
        assert (reader.provider, reader.model, reader.dimension) == ("", "", 0)


def test_mark_indexed_round_trips(store):
    store.mark_indexed(1234.5)
    assert store.last_index_at() == 1234.5


def test_last_index_at_survives_a_corrupt_value(store):
    store._meta_set("last_index_at", "not-a-number")
    assert store.last_index_at() is None


# -- upsert, delete, and FTS rowid parity (criterion B) --------------------------


def test_round_trip_of_every_field(store):
    store.upsert([_rec("c1", E0, source="/notes/x.md", content="hello", heading="Title", level=3, start=7, end=9)])
    hit = store.get_by_chunk_id("c1")
    assert hit is not None
    assert (hit.content, hit.source, hit.heading, hit.heading_level) == ("hello", "/notes/x.md", "Title", 3)
    assert (hit.start_line, hit.end_line, hit.chunk_id, hit.score) == (7, 9, "c1", 0.0)
    assert store.get_by_chunk_id("missing") is None


def test_fts_rowids_track_chunk_ids_through_upsert_reupsert_and_delete(store):
    store.upsert([_rec("c1", E0, content="alpha"), _rec("c2", E1, content="beta"), _rec("c3", E2, content="gamma")])
    assert _chunk_rowids(store) == [1, 2, 3]
    assert _fts_rowids(store) == [1, 2, 3]

    # Re-upserting keeps the row id and replaces the indexed text.
    store.upsert([_rec("c2", E1, content="beta rewritten")])
    assert _chunk_rowids(store) == [1, 2, 3]
    assert _fts_rowids(store) == [1, 2, 3]
    texts = dict(store._conn.execute("SELECT rowid, text FROM chunks_fts"))
    assert texts[2] == "beta rewritten"

    store.delete_chunk_ids(["c1"])
    assert _chunk_rowids(store) == [2, 3]
    assert _fts_rowids(store) == [2, 3]

    store.delete_source("/notes/a.md")
    assert _chunk_rowids(store) == []
    assert _fts_rowids(store) == []


def test_upsert_is_idempotent_and_returns_rows_written(store):
    assert store.upsert([_rec("c1", E0), _rec("c2", E1)]) == 2
    assert store.upsert([_rec("c1", E0)]) == 1
    assert store.count() == 2
    assert store.upsert([]) == 0


def test_upsert_deduplicates_within_one_batch(store):
    assert store.upsert([_rec("c1", E0, content="first"), _rec("c1", E1, content="second")]) == 1
    assert store.count() == 1
    assert _fts_rowids(store) == [1]
    assert store.get_by_chunk_id("c1").content == "second"


def test_upsert_rejects_a_wrong_dimension(store):
    with pytest.raises(IndexMismatch, match="expected 4"):
        store.upsert([_rec("c1", [1.0, 2.0])])


def test_embeddings_are_stored_l2_normalised(store):
    store.upsert([_rec("c1", [3.0, 4.0, 0.0, 0.0])])
    import numpy as np

    blob = store._conn.execute("SELECT embedding FROM chunks").fetchone()[0]
    assert len(blob) == DIM * 4
    assert np.frombuffer(blob, dtype="<f4").tolist() == pytest.approx([0.6, 0.8, 0.0, 0.0])


def test_ids_for_source_and_sources(store):
    store.upsert([_rec("c1", E0), _rec("c2", E1), _rec("c3", E2, source="/notes/b.md")])
    assert set(store.ids_for_source("/notes/a.md")) == {"c1", "c2"}
    assert store.ids_for_source("/notes/a.md")["c1"] == 1
    assert store.ids_for_source("/nope.md") == {}
    assert store.sources() == {"/notes/a.md", "/notes/b.md"}


def test_delete_helpers_report_what_they_removed(store):
    store.upsert([_rec("c1", E0), _rec("c2", E1)])
    assert store.delete_chunk_ids([]) == 0
    assert store.delete_chunk_ids(["missing"]) == 0
    assert store.delete_chunk_ids(["c1", "c1"]) == 1
    assert store.delete_source("/nowhere.md") == 0
    assert store.delete_source("/notes/a.md") == 1


def test_reset_empties_the_index_but_keeps_the_identity(store):
    store.upsert([_rec("c1", E0)])
    store.mark_indexed(99.0)
    store.reset()
    assert store.count() == 0
    assert _fts_rowids(store) == []
    assert store.last_index_at() is None
    assert (store.provider, store.model, store.dimension) == ("fake", "fake-embed", DIM)


def test_many_chunk_ids_are_deleted_across_parameter_batches(store):
    store.upsert([_rec(f"c{i}", E0, start=i, end=i + 1) for i in range(1200)])
    assert store.count() == 1200
    assert store.delete_chunk_ids([f"c{i}" for i in range(1200)]) == 1200
    assert _fts_rowids(store) == []


# -- hybrid search ---------------------------------------------------------------


def test_rrf_scores_are_normalised_to_one_and_a_half(store):
    """A chunk first in both legs scores exactly 1.0; a single leg gives 0.5."""
    store.upsert([
        _rec("c1", E0, content="alpha unique"),
        _rec("c2", E1, content="beta other"),
        _rec("c3", E2, content="gamma other"),
    ])  # fmt: skip

    both = store.search(E0, "unique", top_k=3)
    assert both[0].chunk_id == "c1"
    assert both[0].score == 1.0
    # Second place has the dense leg only, at rank 2.
    assert both[1].score == pytest.approx((1.0 / (RRF_K + 2)) / (2.0 / (RRF_K + 1)))

    dense_only = store.search(E0, "", top_k=3)
    assert dense_only[0].chunk_id == "c1"
    assert dense_only[0].score == 0.5
    assert max(hit.score for hit in dense_only) == 0.5

    keyword_only = store.search([], "unique", top_k=3)
    assert [hit.chunk_id for hit in keyword_only] == ["c1"]
    assert keyword_only[0].score == 0.5


def test_search_respects_top_k(store):
    store.upsert([_rec(f"c{i}", E0, start=i, end=i + 1) for i in range(10)])
    assert len(store.search(E0, "alpha", top_k=3)) == 3
    assert store.search(E0, "alpha", top_k=0) == []


def test_ties_break_deterministically_on_the_row_id(store):
    store.upsert([_rec("c1", E0, content="same"), _rec("c2", E0, content="same"), _rec("c3", E0, content="same")])
    for _ in range(5):
        assert [hit.chunk_id for hit in store.search(E0, "same", top_k=3)] == ["c1", "c2", "c3"]


def test_search_rejects_a_query_vector_of_the_wrong_dimension(store):
    store.upsert([_rec("c1", E0)])
    with pytest.raises(IndexMismatch, match="query vector has 2 dimensions"):
        store.search([1.0, 0.0], "alpha")


def test_a_corrupt_embedding_blob_is_reported_as_a_mismatch(store):
    store.upsert([_rec("c1", E0)])
    with store._write() as conn:
        conn.execute("UPDATE chunks SET embedding = ? WHERE chunk_id = 'c1'", (b"\x00" * 12,))
    with pytest.raises(IndexMismatch, match="Rebuild it"):
        store.search(E0, "")


def test_cjk_text_is_searchable_per_character(store):
    store.upsert([_rec("c1", E0, content="北京的天气很好"), _rec("c2", E1, content="alpha beta")])
    assert [hit.chunk_id for hit in store.search([], "北京", top_k=5)] == ["c1"]
    assert store.search([], "东京", top_k=5) == []
    assert [hit.chunk_id for hit in store.search([], "天气", top_k=5)] == ["c1"]


def test_fts_text_isolates_cjk_and_leaves_latin_alone():
    assert fts_text("alpha beta") == "alpha beta"
    assert fts_text("北京x").split() == ["北", "京", "x"]
    assert fts_text("こんにちは").split() == list("こんにちは")


def test_fts_query_quotes_every_term_and_neutralises_the_grammar():
    assert fts_query("alpha beta") == '"alpha" OR "beta"'
    assert fts_query("北京") == '"北 京"'
    assert fts_query('say "hi"') == '"say" OR """hi"""'
    assert fts_query("   ") == ""
    assert fts_query("") == ""


@pytest.mark.parametrize("query", ["*", '"', "AND", "OR", "NOT", "NEAR(a b)", "^foo", "col:x", "((", "-", "a*"])
def test_pathological_queries_never_raise(store, query):
    store.upsert([_rec("c1", E0, content="alpha beta")])
    assert isinstance(store.search(E0, query, top_k=3), list)


def test_no_fts_environment_switch_degrades_to_dense_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(tmp_path / "index.db", allow_rebuild=True) as opened:
        assert opened.fts_enabled is False
        opened.upsert([_rec("c1", E0, content="alpha"), _rec("c2", E1, content="beta")])
        assert not opened._table_exists("chunks_fts")
        assert [hit.chunk_id for hit in opened.search(E0, "alpha", top_k=2)] == ["c1", "c2"]
        assert opened.search([], "alpha") == []


class _RefusingConnection:
    """Stands in for a sqlite3 connection whose build has no FTS5 module."""

    in_transaction = True  # keeps Store._write() from issuing BEGIN/COMMIT

    def __init__(self, message: str = "no such module: fts5") -> None:
        self._message = message

    def execute(self, sql, *args, **kwargs):
        raise sqlite3.OperationalError(self._message)


def _detached(tmp_path, message: str = "no such module: fts5", *, table_exists: bool = False) -> Store:
    store = Store(_RefusingConnection(message), tmp_path / "index.db")
    store._table_exists = lambda name: table_exists  # type: ignore[method-assign]
    return store


def test_reopening_with_fts_backfills_a_database_indexed_without_it(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0, content="alpha unique"), _rec("c2", E1, content="beta")])
        assert not opened._table_exists("chunks_fts")

    monkeypatch.delenv("MEMSEARCH_MINI_NO_FTS")
    with Store.open(path) as reopened:
        assert reopened.fts_enabled is True
        assert _fts_rowids(reopened) == [1, 2]
        assert [hit.chunk_id for hit in reopened.search([], "unique")] == ["c1"]


def test_reopening_with_fts_reconciles_rows_a_no_fts_run_never_wrote(tmp_path, monkeypatch):
    """An existing table is reconciled, not just probed: NO_FTS=1 desyncs it permanently."""
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0, content="alpha")])

    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(path) as blind:
        assert blind.fts_enabled is False
        blind.upsert([_rec("c2", E1, content="beta unique")])

    monkeypatch.delenv("MEMSEARCH_MINI_NO_FTS")
    with Store.open(path) as reopened:
        assert _fts_rowids(reopened) == _chunk_rowids(reopened)
        assert [hit.chunk_id for hit in reopened.search([], "unique")] == ["c2"]


def test_reset_without_fts_leaves_no_row_behind_to_shadow_a_later_chunk(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0, content="alpha"), _rec("c2", E1, content="beta")])

    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(path) as blind:
        blind.reset()
        blind.upsert([_rec("c3", E2, content="gamma unique")])

    monkeypatch.delenv("MEMSEARCH_MINI_NO_FTS")
    with Store.open(path) as reopened:
        assert _fts_rowids(reopened) == _chunk_rowids(reopened)
        assert [hit.chunk_id for hit in reopened.search([], "unique")] == ["c3"]
        assert reopened.search([], "alpha") == []  # the emptied rows must not answer for c3


def test_deleting_without_fts_leaves_no_row_behind_to_shadow_a_later_chunk(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0, content="alpha")])

    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(path) as blind:
        assert blind.delete_chunk_ids(["c1"]) == 1
        blind.upsert([_rec("c2", E1, content="beta unique")])

    monkeypatch.delenv("MEMSEARCH_MINI_NO_FTS")
    with Store.open(path) as reopened:
        assert _fts_rowids(reopened) == _chunk_rowids(reopened)
        assert [hit.chunk_id for hit in reopened.search([], "unique")] == ["c2"]
        assert reopened.search([], "alpha") == []


def _drifted(path: Path, monkeypatch) -> None:
    """One chunk with its keyword row, one written blind: the two tables now disagree."""
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0, content="alpha")])

    monkeypatch.setenv("MEMSEARCH_MINI_NO_FTS", "1")
    with _open(path) as blind:
        blind.upsert([_rec("c2", E1, content="beta unique")])
    monkeypatch.delenv("MEMSEARCH_MINI_NO_FTS")


class _LosesTheRace(Store):
    """The second opener of a drifted index: another process repairs the very same
    drift between this one's unlocked probe and its own BEGIN IMMEDIATE."""

    raced = False

    @contextmanager
    def _write(self):
        if not self.raced:
            self.raced = True
            Store.open(self.path).close()  # the winner, under its own write lock
        with super()._write() as conn:
            yield conn


def test_a_drift_another_process_repaired_first_is_a_no_op(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    _drifted(path, monkeypatch)
    loser = _LosesTheRace(sqlite3.connect(str(path), timeout=10.0, isolation_level=None), path)

    with loser:  # re-inserting a live rowid would raise sqlite3.IntegrityError
        assert loser._resolve_fts() is True
        assert loser.raced  # the repair path really ran
        assert _fts_rowids(loser) == _chunk_rowids(loser)


def test_a_drifted_index_on_a_read_only_file_still_opens_and_searches(tmp_path, monkeypatch):
    """Nothing can repair a read-only copy; the keyword rows it has stay usable."""
    path = tmp_path / "index.db"
    _drifted(path, monkeypatch)
    path.chmod(0o444)
    try:
        with Store.open(path) as reader:
            assert reader.fts_enabled is True
            assert [hit.chunk_id for hit in reader.search([], "alpha")] == ["c1"]
            assert [hit.chunk_id for hit in reader.search(E1, "", top_k=1)] == ["c2"]
    finally:
        path.chmod(0o644)  # the drift heals the next time a writer opens it


def test_creating_the_fts_table_without_fts5_is_reported_clearly(tmp_path):
    with pytest.raises(StoreError, match="MEMSEARCH_MINI_NO_FTS=1"):
        _detached(tmp_path)._resolve_fts()


def test_probing_an_existing_fts_table_without_fts5_is_reported_clearly(tmp_path):
    with pytest.raises(StoreError, match="MEMSEARCH_MINI_NO_FTS=1"):
        _detached(tmp_path, table_exists=True)._resolve_fts()


def test_other_sqlite_errors_from_the_fts_table_are_not_swallowed(tmp_path):
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _detached(tmp_path, "disk I/O error")._resolve_fts()


class _FlakyCommit:
    """A connection whose first COMMIT fails, as a busy database's would."""

    def __init__(self, conn: sqlite3.Connection, failures: int = 1) -> None:
        self._conn = conn
        self.failures = failures

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def commit(self) -> None:
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        self._conn.commit()


def test_a_failed_commit_rolls_back_instead_of_stranding_the_transaction(tmp_path):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened._conn = flaky = _FlakyCommit(opened._conn)

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            opened.upsert([_rec("c1", E0, content="alpha")])

        assert flaky.in_transaction is False  # the next write must be able to BEGIN
        assert opened.upsert([_rec("c2", E1, content="beta")]) == 1

    with Store.open(path) as reader:
        assert [hit.chunk_id for hit in reader.search(E1, "beta")] == ["c2"]


def test_the_matrix_cache_notices_writes_from_another_connection(tmp_path):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as writer, Store.open(path) as reader:
        writer.upsert([_rec("c1", E0)])
        assert [hit.chunk_id for hit in reader.search(E0, "")] == ["c1"]
        writer.upsert([_rec("c2", E1, content="beta")])
        assert {hit.chunk_id for hit in reader.search(E0, "")} == {"c1", "c2"}


# -- mismatch and rebuild (criterion E) ------------------------------------------


def test_reopening_with_another_model_raises_without_allow_rebuild(tmp_path):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0)])
    with pytest.raises(IndexMismatch) as excinfo:
        _open(path, model="other-model")
    assert "fake-embed" in str(excinfo.value)
    assert "memsearch-mini index --force" in str(excinfo.value)


def test_reopening_with_allow_rebuild_empties_the_index_and_updates_meta(tmp_path, capsys):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0)])
        opened.mark_indexed(5.0)

    with _open(path, provider="other", model="other-model", dimension=8, allow_rebuild=True) as rebuilt:
        assert rebuilt.rebuilt is True
        assert rebuilt.count() == 0
        assert (rebuilt.provider, rebuilt.model, rebuilt.dimension) == ("other", "other-model", 8)
        assert rebuilt.last_index_at() is None
    assert "rebuilding the index" in capsys.readouterr().err


def test_an_empty_index_adopts_a_new_model_without_rebuilding(tmp_path):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True):
        pass
    with _open(path, model="other-model") as opened:
        assert opened.rebuilt is False
        assert opened.model == "other-model"


def test_an_obsolete_schema_version_is_rebuilt(tmp_path, capsys):
    path = tmp_path / "index.db"
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec("c1", E0)])
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        raw.commit()

    with _open(path, allow_rebuild=True) as reopened:
        assert reopened.rebuilt is True
        assert reopened.count() == 0
        assert reopened._meta_get("schema_version") == str(SCHEMA_VERSION)
    assert "schema is obsolete" in capsys.readouterr().err
    assert path.is_file()  # rebuilt in place, never unlinked


# -- concurrency (criterion F) ---------------------------------------------------


def _seed(path: Path, count: int = 5) -> None:
    with _open(path, allow_rebuild=True) as opened:
        opened.upsert([_rec(f"seed{i}", E0, start=i, end=i + 1, content="seed alpha") for i in range(count)])


def test_a_reader_thread_searches_through_a_long_write_transaction(tmp_path):
    """The reader must open and search *inside* the writer's transaction.

    The writer only commits once the reader reports a completed search, so a
    query-mode open that needed the write lock would deadlock here instead of
    passing by luck.
    """
    path = tmp_path / "index.db"
    _seed(path)
    errors: list[BaseException] = []
    searches: list[int] = []
    writing, reader_ready, finished = threading.Event(), threading.Event(), threading.Event()

    def write() -> None:
        try:
            with _open(path) as writer, writer._write():
                writing.set()
                for i in range(500):
                    writer.upsert([_rec(f"w{i}", E1, start=i, end=i + 1, content="written alpha")])
                assert reader_ready.wait(30)
                time.sleep(0.1)
        except BaseException as exc:  # re-raised through the assertion below
            errors.append(exc)
        finally:
            finished.set()

    def read() -> None:
        try:
            assert writing.wait(30)
            with Store.open(path) as reader:
                done = 0
                while done < 5000:
                    reader.search(E0, "alpha", top_k=3)
                    done += 1
                    reader_ready.set()
                    if finished.is_set():
                        break
                searches.append(done)
        except BaseException as exc:  # re-raised through the assertion below
            errors.append(exc)
        finally:
            reader_ready.set()

    threads = [threading.Thread(target=write), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert searches and searches[0] > 0
    with Store.open(path) as final:
        assert final.count() == 505


_READER_SCRIPT = """
import json, os, sys, time
from memsearch_mini.store import Store

db, ready, done = sys.argv[1], sys.argv[2], sys.argv[3]
searches = 0
with Store.open(db) as store:
    deadline = time.time() + 60
    while time.time() < deadline:
        store.search([1.0, 0.0, 0.0, 0.0], "alpha", top_k=3)
        searches += 1
        open(ready, "w").close()
        if searches >= 20 and os.path.exists(done):
            break
sys.stdout.write(json.dumps({"searches": searches}))
"""


def test_a_second_process_searches_through_a_long_write_transaction(tmp_path):
    """Same guarantee as the thread test, across processes: the child opens the
    database and searches while the parent holds an uncommitted write."""
    path = tmp_path / "index.db"
    _seed(path)
    script = tmp_path / "reader.py"
    script.write_text(_READER_SCRIPT, encoding="utf-8")
    ready, done = tmp_path / "reader.ready", tmp_path / "writer.done"
    reader = None
    try:
        with _open(path) as writer, writer._write():
            reader = subprocess.Popen(
                [sys.executable, str(script), str(path), str(ready), str(done)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(500):
                writer.upsert([_rec(f"w{i}", E1, start=i, end=i + 1, content="written alpha")])
            deadline = time.time() + 60
            while not ready.exists() and reader.poll() is None and time.time() < deadline:
                time.sleep(0.02)
            assert ready.exists(), f"reader never completed a search: {reader.communicate()[1]}"
            done.write_text("go", encoding="utf-8")
            time.sleep(0.1)
        out, err = reader.communicate(timeout=120)
    finally:
        if reader is not None and reader.poll() is None:
            reader.kill()
            reader.communicate()
    assert reader.returncode == 0, err
    assert json.loads(out)["searches"] >= 20
    with Store.open(path) as final:
        assert final.count() == 505


# -- performance -----------------------------------------------------------------


def test_search_stays_fast_with_five_thousand_chunks(tmp_path):
    import numpy as np

    rng = np.random.default_rng(1234)
    vectors = rng.random((5000, 256), dtype=np.float32)
    with Store.open(tmp_path / "big.db", provider="fake", model="m", dimension=256, allow_rebuild=True) as opened:
        opened.upsert([
            _rec(f"c{i}", vectors[i].tolist(), start=i, end=i + 1, content=f"chunk {i} alpha beta gamma")
            for i in range(5000)
        ])  # fmt: skip
        query = vectors[7].tolist()
        opened.search(query, "alpha", top_k=5)  # warm the matrix cache
        started = time.perf_counter()
        for _ in range(3):
            hits = opened.search(query, "alpha", top_k=5)
        elapsed = (time.perf_counter() - started) / 3
    assert hits[0].chunk_id == "c7"
    assert elapsed < 2.0, f"search took {elapsed * 1000:.0f} ms"


# -- index_paths -----------------------------------------------------------------


def _journal(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


async def test_index_paths_indexes_a_directory_and_records_the_run(tmp_path, fake_embedder):
    memory = tmp_path / "memory"
    _journal(memory, "2026-01-01.md", "# Day one\n\n## Session 10:00\n\n- did a thing\n")
    _journal(memory, "2026-01-02.md", "# Day two\n\n## Session 11:00\n\n- did another thing\n")
    before = time.time()
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        result = await index_paths(opened, fake_embedder, [memory])
        assert result.total_files == 2
        assert result.indexed_chunks == opened.count() > 0
        assert result.failed_files == ()
        assert result.rebuilt is False
        assert before <= opened.last_index_at() <= time.time()
        assert opened.sources() == {str(memory / "2026-01-01.md"), str(memory / "2026-01-02.md")}


async def test_index_paths_is_incremental_and_force_re_embeds(tmp_path, fake_embedder):
    memory = tmp_path / "memory"
    _journal(memory, "log.md", "# Log\n\n## One\n\n- first note\n")
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        first = await index_paths(opened, fake_embedder, [memory])
        assert first.indexed_chunks > 0

        fake_embedder.calls.clear()
        again = await index_paths(opened, fake_embedder, [memory])
        assert again.indexed_chunks == 0
        assert fake_embedder.calls == []

        forced = await index_paths(opened, fake_embedder, [memory], force=True)
        assert forced.indexed_chunks == first.indexed_chunks
        assert fake_embedder.calls != []


async def test_index_paths_drops_chunks_that_left_a_file(tmp_path, fake_embedder):
    memory = tmp_path / "memory"
    path = _journal(memory, "log.md", "# Log\n\n## One\n\n- first\n\n## Two\n\n- second\n")
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        await index_paths(opened, fake_embedder, [memory])
        contents = {hit.content for hit in [opened.get_by_chunk_id(cid) for cid in opened.ids_for_source(str(path))]}
        assert any("second" in content for content in contents)

        path.write_text("# Log\n\n## One\n\n- first\n", encoding="utf-8")
        await index_paths(opened, fake_embedder, [memory])
        contents = {hit.content for hit in [opened.get_by_chunk_id(cid) for cid in opened.ids_for_source(str(path))]}
        assert not any("second" in content for content in contents)


async def test_index_paths_prunes_deleted_files_only_under_directory_roots(tmp_path, fake_embedder):
    memory = tmp_path / "memory"
    gone = _journal(memory, "gone.md", "# Gone\n\n- vanishing note\n")
    kept = _journal(memory, "kept.md", "# Kept\n\n- staying note\n")
    outside = _journal(tmp_path / "elsewhere", "outside.md", "# Outside\n\n- external note\n")
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        await index_paths(opened, fake_embedder, [memory, outside])
        assert opened.sources() == {str(gone), str(kept), str(outside)}

        gone.unlink()
        outside.unlink()
        # ``outside`` was indexed as an explicit file root, so it must survive.
        await index_paths(opened, fake_embedder, [memory, outside])
        assert opened.sources() == {str(kept), str(outside)}


async def test_index_paths_collects_per_file_failures_without_stopping(tmp_path, fake_embedder, monkeypatch):
    memory = tmp_path / "memory"
    _journal(memory, "aaa.md", "# A\n\n- first note\n")
    _journal(memory, "bbb.md", "# B\n\n- broken note\n")
    _journal(memory, "ccc.md", "# C\n\n- third note\n")
    real_read = __import__("memsearch_mini.scanner", fromlist=["x"]).read_utf8_text_replace

    def read(path):
        if str(path).endswith("bbb.md"):
            raise OSError("disk gremlin")
        return real_read(path)

    monkeypatch.setattr("memsearch_mini.scanner.read_utf8_text_replace", read)
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        result = await index_paths(opened, fake_embedder, [memory])
    assert result.total_files == 3
    assert result.indexed_chunks > 0
    assert len(result.failed_files) == 1
    failed_path, message = result.failed_files[0]
    assert failed_path.endswith("bbb.md")
    assert message == "OSError: disk gremlin"


async def test_index_paths_skips_files_over_the_size_limit(tmp_path, fake_embedder, monkeypatch):
    memory = tmp_path / "memory"
    _journal(memory, "huge.md", "# Huge\n\n" + "- a long note\n" * 500)
    monkeypatch.setenv("MEMSEARCH_MINI_MAX_FILE_MB", "0.001")
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        result = await index_paths(opened, fake_embedder, [memory])
        assert opened.count() == 0
    assert len(result.failed_files) == 1
    assert "MEMSEARCH_MINI_MAX_FILE_MB" in result.failed_files[0][1]


async def test_index_paths_survives_broken_encoding_and_nul_bytes(tmp_path, fake_embedder):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "raw.md").write_bytes(b"# Raw\n\n- caf\xe9 note with a \x00 nul\n")
    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        result = await index_paths(opened, fake_embedder, [memory])
        assert result.failed_files == ()
        assert result.indexed_chunks == 1
        contents = [opened.get_by_chunk_id(cid).content for cid in opened.ids_for_source(str(memory / "raw.md"))]
    assert "\x00" not in contents[0]
    assert "�" in contents[0]


async def test_index_paths_batches_by_the_embedder_batch_size(tmp_path, make_embedder):
    memory = tmp_path / "memory"
    body = "".join(f"## Section {i}\n\n- note number {i}\n\n" for i in range(10))
    _journal(memory, "log.md", body)
    embedder = make_embedder(batch_size=4)
    with _open(tmp_path / "index.db", model=embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        result = await index_paths(opened, embedder, [memory])
    assert result.indexed_chunks == 10
    assert embedder.call_sizes == [4, 4, 2]


async def test_index_paths_marks_the_run_before_doing_the_work(tmp_path, fake_embedder, monkeypatch):
    memory = tmp_path / "memory"
    _journal(memory, "log.md", "# Log\n\n- a note\n")
    seen: list[float | None] = []
    real_scan = __import__("memsearch_mini.scanner", fromlist=["x"]).scan_paths

    with _open(tmp_path / "index.db", model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:

        def scan(paths, **kwargs):
            seen.append(opened.last_index_at())
            return real_scan(paths, **kwargs)

        monkeypatch.setattr("memsearch_mini.scanner.scan_paths", scan)
        await index_paths(opened, fake_embedder, [memory])
    assert seen and seen[0] is not None


async def test_index_paths_reports_the_rebuild_that_opening_performed(tmp_path, fake_embedder):
    path = tmp_path / "index.db"
    memory = tmp_path / "memory"
    _journal(memory, "log.md", "# Log\n\n- a note\n")
    with _open(path, model=fake_embedder.model_name, dimension=8, allow_rebuild=True) as opened:
        await index_paths(opened, fake_embedder, [memory])
    with _open(path, provider="other", model="other-model", dimension=8, allow_rebuild=True) as reopened:
        result = await index_paths(reopened, fake_embedder, [memory])
    assert result.rebuilt is True
