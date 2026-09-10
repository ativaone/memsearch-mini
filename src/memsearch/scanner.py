"""Filesystem scan for the markdown files that make up a memory index."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    mtime: float
    size: int


def scan_paths(paths: list[str | Path], *, extensions: tuple[str, ...] = (".md", ".markdown"),
               ignore_hidden: bool = True) -> list[ScannedFile]:  # fmt: skip
    """Recursively collect markdown files from *paths* (files or directories).

    Hidden entries are skipped unless *ignore_hidden* is False; results are
    deduplicated by resolved path and sorted.
    """
    results: list[ScannedFile] = []
    seen: set[str] = set()
    for raw in paths:
        root = Path(raw).expanduser().resolve()
        if root.is_file():
            _maybe_add(root, extensions, seen, results)
        elif root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                if ignore_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not (ignore_hidden and name.startswith(".")):
                        _maybe_add(Path(dirpath) / name, extensions, seen, results)
    results.sort(key=lambda f: f.path)
    return results


def _maybe_add(path: Path, extensions: tuple[str, ...], seen: set[str], results: list[ScannedFile]) -> None:
    resolved = str(path.resolve())
    if path.suffix.lower() not in extensions or resolved in seen:
        return
    seen.add(resolved)
    stat = path.stat()
    results.append(ScannedFile(path=path, mtime=stat.st_mtime, size=stat.st_size))


def read_utf8_text_replace(path: str | Path) -> str:
    """Read text as UTF-8, replacing invalid bytes and dropping NULs.

    Agent hooks occasionally append malformed tool output to a journal.
    """
    return Path(path).read_bytes().decode("utf-8", errors="replace").replace("\x00", "")
