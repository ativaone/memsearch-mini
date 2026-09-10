"""The ``memsearch-mini`` command line.

Module scope stays light (click, stdlib, and the dependency-free ``config`` and
``hooks``); numpy, the store and the embedding backends load inside the commands
that use them.  Exit codes: 0 ok · 1 error · 2 usage · 3 unknown transcript format.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

import click

from . import config, hooks

# Journal anchors: Claude Code writes turn: and transcript:, Codex only rollout:.
_ANCHOR = re.compile(r"<!--\s*session:(?P<session>\S+)(?:\s+turn:(?P<turn>\S+))?"
                     r"\s+(?P<kind>transcript|rollout):(?P<path>\S+)\s*-->")  # fmt: skip


def _configure_cli_streams() -> None:
    """Configure CLI output streams for reliable Unicode output."""
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


class _MemSearchMiniGroup(click.Group):
    """Configure CLI streams before Click parses arguments or exits eagerly."""

    def main(self, *args, **kwargs):
        _configure_cli_streams()
        return super().main(*args, **kwargs)


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(1)


def _locations() -> tuple[Path, Path]:
    """``(.memsearch-mini directory, index database)`` for the current project."""
    mdir = hooks.memsearch_mini_dir(hooks.resolve_project_dir({}))
    return mdir, mdir / "index.db"


def _embedder_for(cfg: config.Config, provider: str = "", model: str = "") -> Any:
    """Build the embedder — the single seam between the CLI and a real model.
    *provider*/*model* come from the index's identity, so search survives a config change."""
    from .embeddings import get_provider

    return get_provider(
        provider or cfg.embedding.provider,
        model=(model or cfg.embedding.model) or None,
        batch_size=cfg.embedding.batch_size,
        base_url=cfg.embedding.base_url or None,
        api_key=cfg.embedding.api_key or None,
    )


@contextmanager
def _index_lock(mdir: Path, skip_if_locked: bool) -> Iterator[bool]:
    """Hold the project's index lock; yields False when another indexer has it."""
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / "index.lock", "a") as handle:  # closing it releases the lock
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if skip_if_locked:
                yield False
                return
            click.echo("[memsearch-mini] another indexer is running; waiting for it", err=True)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield True


@click.group(cls=_MemSearchMiniGroup)
@click.version_option(package_name="memsearch-mini")
def cli() -> None:
    """memsearch-mini — persistent memory for Claude Code and Codex."""


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Re-embed every chunk, not just the new ones.")
@click.option("--skip-if-locked", is_flag=True, help="Exit quietly when another indexer holds the lock.")
def index(paths: tuple[str, ...], force: bool, skip_if_locked: bool) -> None:
    """Index PATHS, defaulting to this project's memory directory."""
    import asyncio

    from .store import Store, StoreError, index_paths

    mdir, db = _locations()
    if not paths:
        (mdir / "memory").mkdir(parents=True, exist_ok=True)
    targets: list[str | Path] = list(paths) or [mdir / "memory"]
    cfg = config.load()
    with _index_lock(mdir, skip_if_locked) as acquired:
        if not acquired:
            return  # another indexer is already doing this work
        try:
            embedder = _embedder_for(cfg)
            with Store.open(db, provider=cfg.embedding.provider, model=embedder.model_name,
                            dimension=embedder.dimension, allow_rebuild=True) as store:  # fmt: skip
                result = asyncio.run(index_paths(store, embedder, targets, force=force,
                                                 max_chunk_size=cfg.chunking.max_chunk_size,
                                                 overlap_lines=cfg.chunking.overlap_lines,
                                                 min_chunk_size=cfg.chunking.min_chunk_size))  # fmt: skip
        except (StoreError, ImportError, ValueError) as exc:
            _fail(f"Error: {exc}")
    failed = result.failed_files
    click.echo(f"Indexed {result.indexed_chunks} chunks from {result.total_files} files"
               + (f" ({len(failed)} files failed)" if failed else ""))  # fmt: skip
    click.echo("".join(f"  {source}: {reason}\n" for source, reason in failed), nl=False, err=True)
    if failed and len(failed) == result.total_files:
        raise SystemExit(1)  # nothing at all could be indexed


@cli.command()
@click.argument("query")
@click.option("--top-k", "-k", default=5, type=int, help="Number of results.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def search(query: str, top_k: int, json_output: bool) -> None:
    """Search the index for QUERY."""
    import asyncio

    from .store import Store, StoreError

    _mdir, db = _locations()
    cfg = config.load()
    try:
        with Store.open(db) as store:
            if store.count() == 0:
                click.echo("[]" if json_output else "No results (index is empty)")
                return
            embedder = _embedder_for(cfg, provider=store.provider, model=store.model)
            vector = asyncio.run(embedder.embed([query]))[0]
            hits = store.search(vector, query, top_k=top_k)
    except StoreError as exc:
        _fail(f"Error: {exc}")
    if json_output:
        click.echo(json.dumps([hit.to_dict() for hit in hits], indent=2, ensure_ascii=False))
        return
    if not hits:
        click.echo("No results found.")
    for position, hit in enumerate(hits, 1):
        body = hit.content
        if len(body) > 500:  # a long chunk is shown in full by 'expand'
            body = f"{body[:500]}\n  ... [truncated, run 'memsearch-mini expand {hit.chunk_id}' for full content]"
        heading = f"Heading: {hit.heading}\n" if hit.heading else ""
        click.echo(f"\n--- Result {position} (score: {hit.score:.4f}) ---\nSource: {hit.source}\n{heading}{body}")


@cli.command()
@click.argument("chunk_id")
@click.option("--lines", "-n", default=None, type=int, help="Show N lines around the chunk instead of its section.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def expand(chunk_id: str, lines: int | None, json_output: bool) -> None:
    """Show the full journal section around CHUNK_ID."""
    from .scanner import read_utf8_text_replace
    from .store import Store, StoreError

    _mdir, db = _locations()
    try:
        with Store.open(db) as store:
            hit = store.get_by_chunk_id(chunk_id)
    except StoreError as exc:
        _fail(f"Error: {exc}")
    if hit is None:
        _fail(f"Chunk not found: {chunk_id}")
    try:
        all_lines = read_utf8_text_replace(hit.source).splitlines()
    except OSError as exc:
        _fail(f"Error: {exc}")
    if lines is None:
        content, first, last = _extract_section(all_lines, hit.start_line, hit.heading_level)
    else:
        top, bottom = max(0, hit.start_line - 1 - lines), min(len(all_lines), hit.end_line + lines)
        content, first, last = "\n".join(all_lines[top:bottom]), top + 1, bottom
    match = _ANCHOR.search(content)
    anchor = ({"session": match["session"], "turn": match["turn"] or "", "kind": match["kind"],
               "transcript": match["path"]} if match else None)  # fmt: skip
    if json_output:
        click.echo(json.dumps({"chunk_id": chunk_id, "source": hit.source, "heading": hit.heading,
                               "start_line": first, "end_line": last, "content": content,
                               "anchor": anchor}, indent=2, ensure_ascii=False))  # fmt: skip
        return
    heading = f"Heading: {hit.heading}\n" if hit.heading else ""
    turn = f" turn:{anchor['turn']}" if anchor and anchor["turn"] else ""
    label = f"Anchor: session:{anchor['session']}{turn} {anchor['kind']}:{anchor['transcript']}\n" if anchor else ""
    click.echo(f"Source: {hit.source} (lines {first}-{last})\n{heading}{label}\n{content}")


def _extract_section(all_lines: list[str], start_line: int, heading_level: int) -> tuple[str, int, int]:
    """The chunk's whole section as ``(text, first line, last line)``: back to its
    own heading, forward to the next heading of equal or higher level."""

    def bound(indices: range, fallback: int) -> int:
        for i in indices:
            line = all_lines[i]
            if line.startswith("#") and len(line) - len(line.lstrip("#")) <= heading_level:
                return i
        return fallback

    # With heading_level 0 no line ever satisfies bound(), so both fall back.
    start = bound(range(start_line - 2, -1, -1), start_line - 1)  # 0-indexed
    end = bound(range(start_line, len(all_lines)), len(all_lines))
    return "\n".join(all_lines[start:end]), start + 1, end


@cli.command("transcript")
@click.argument("path", type=click.Path())
@click.option("--turn", "-t", default=None, help="Target turn id (prefix match); shows surrounding context.")
@click.option("--context", "-c", default=3, type=int, help="Turns of context to show before/after --turn.")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def transcript_cmd(path: str, turn: str | None, context: int, json_output: bool) -> None:
    """Show the original conversation turns, with tool calls, from a transcript.
    L3 of recall: pass the path from a journal anchor to recover exact commands."""
    from . import transcript as transcript_mod

    try:
        turns = transcript_mod.parse_transcript(path)
    except transcript_mod.UnknownTranscriptFormat:
        click.echo("Unrecognized transcript format. Read the file directly to locate the relevant turns.", err=True)
        raise SystemExit(3) from None
    except OSError as exc:
        _fail(f"Error: {exc}")
    turns = transcript_mod.select_turns(turns, turn, context)
    if not json_output:
        click.echo(transcript_mod.format_turns(turns))
        return
    payload = [{"role": t.role, "uuid": t.uuid, "text": t.text,
                "tools": [{"name": c.name, "command": c.command, "output": c.output} for c in t.tools]}
               for t in turns]  # fmt: skip
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _render(value: Any) -> str:  # TOML-ish scalars: booleans lowercase, the rest as text
    return ("false", "true")[value] if isinstance(value, bool) else str(value)


@cli.group("config")
def config_group() -> None:
    """Read and write ~/.memsearch-mini/config.toml."""


@config_group.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Print the effective value of KEY, for example embedding.provider."""
    try:
        click.echo(_render(config.get_value(key)))
    except (KeyError, ValueError) as exc:
        _fail(f"Error: {exc.args[0] if exc.args else exc}")


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Store VALUE under KEY."""
    try:
        stored = config.set_value(key, value)
    except (KeyError, ValueError, OSError) as exc:
        _fail(f"Error: {exc.args[0] if exc.args else exc}")
    click.echo(f"{key} = {_render(stored)}")


@config_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def config_list(json_output: bool) -> None:
    """Print the effective configuration."""
    data = config.to_dict(config.load())
    if json_output:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    click.echo("\n".join(f"{section}.{key} = {_render(value)}"
                         for section, values in data.items() for key, value in values.items()))  # fmt: skip


@cli.command()
def stats() -> None:
    """Show what the local index holds."""
    from .store import Store, StoreError

    mdir, db = _locations()
    cfg = config.load()
    try:
        with Store.open(db) as store:
            count, sources, last = store.count(), len(store.sources()), store.last_index_at()
            provider, model, dimension = store.provider, store.model, store.dimension
    except StoreError as exc:
        _fail(f"Error: {exc}")
    embedding = f"{provider or cfg.embedding.provider}/{model or cfg.effective_model()} (dimension {dimension})"
    stamp = datetime.fromtimestamp(last).isoformat(timespec="seconds") if last else "never"
    click.echo(f"Index: {db}\nChunks: {count}\nSources: {sources}\nEmbedding: {embedding}\n"
               f"Last indexed: {stamp}\nMemory dir: {mdir / 'memory'}")  # fmt: skip


@cli.command()
@click.confirmation_option("--yes", prompt="Delete every chunk from the index?")
def reset() -> None:
    """Empty the index. The markdown journals are untouched."""
    from .store import Store, StoreError

    _mdir, db = _locations()
    try:
        with Store.open(db) as store:
            store.reset()
    except StoreError as exc:
        _fail(f"Error: {exc}")
    click.echo("Index cleared")


cli.add_command(hooks.hook)
