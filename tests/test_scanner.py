"""Tests for the markdown file scanner."""

from __future__ import annotations

from pathlib import Path

from memsearch.scanner import ScannedFile, read_utf8_text_replace, scan_paths


def test_scan_finds_markdown_files_recursively(tmp_path: Path):
    (tmp_path / "a.md").write_text("# A")
    (tmp_path / "b.markdown").write_text("# B")
    (tmp_path / "c.txt").write_text("not markdown")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.MD").write_text("# D")

    results = scan_paths([tmp_path])
    assert [r.path.name for r in results] == ["a.md", "b.markdown", "d.MD"]
    assert all(isinstance(r, ScannedFile) and r.size > 0 and r.mtime > 0 for r in results)


def test_scan_ignores_hidden_files_and_directories(tmp_path: Path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.md").write_text("# secret")
    (tmp_path / ".dotfile.md").write_text("# dot")
    (tmp_path / "visible.md").write_text("# visible")

    assert [r.path.name for r in scan_paths([tmp_path])] == ["visible.md"]


def test_scan_includes_hidden_entries_when_asked(tmp_path: Path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.md").write_text("# secret")
    (tmp_path / ".dotfile.md").write_text("# dot")

    names = {r.path.name for r in scan_paths([tmp_path], ignore_hidden=False)}
    assert names == {"secret.md", ".dotfile.md"}


def test_scan_accepts_explicit_files_and_custom_extensions(tmp_path: Path):
    note = tmp_path / "single.md"
    note.write_text("# Single")
    text = tmp_path / "notes.txt"
    text.write_text("plain")

    assert [r.path for r in scan_paths([note])] == [note]
    assert scan_paths([text]) == []
    assert [r.path for r in scan_paths([text], extensions=(".txt",))] == [text]


def test_scan_deduplicates_and_sorts(tmp_path: Path):
    (tmp_path / "b.md").write_text("# B")
    (tmp_path / "a.md").write_text("# A")

    results = scan_paths([tmp_path / "b.md", tmp_path, tmp_path / "b.md"])
    assert [r.path.name for r in results] == ["a.md", "b.md"]


def test_scan_skips_paths_that_do_not_exist(tmp_path: Path):
    assert scan_paths([tmp_path / "nowhere", tmp_path / "nowhere.md"]) == []
    assert scan_paths([]) == []


def test_read_utf8_text_replace_survives_bad_bytes_and_nuls(tmp_path: Path):
    path = tmp_path / "broken.md"
    path.write_bytes(b"caf\xe9 \x00 done\n")
    text = read_utf8_text_replace(path)
    assert "\x00" not in text
    assert text.startswith("caf")
    assert text.endswith(" done\n")
    assert "�" in text


def test_read_utf8_text_replace_keeps_valid_text_intact(tmp_path: Path):
    path = tmp_path / "ok.md"
    path.write_text("# 北京\n\n- é ok\n", encoding="utf-8")
    assert read_utf8_text_replace(path) == "# 北京\n\n- é ok\n"
