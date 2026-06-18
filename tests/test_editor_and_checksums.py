"""Phase 3a: editor launch + content checksum (FR-1.3, FR-2.1)."""

from __future__ import annotations

from pathlib import Path

from lazyserver.backup.checksums import sha256_of
from lazyserver.config import Settings
from lazyserver.editor import launch_editor


def test_sha256_of_missing_returns_none(tmp_path: Path):
    assert sha256_of(tmp_path / "nope") is None


def test_sha256_of_existing_is_stable(tmp_path: Path):
    p = tmp_path / "x"
    p.write_text("hello\n", encoding="utf-8")
    first = sha256_of(p)
    second = sha256_of(p)
    assert first == second
    assert isinstance(first, str) and len(first) == 64


def test_sha256_of_changes_with_content(tmp_path: Path):
    p = tmp_path / "x"
    p.write_text("hello\n", encoding="utf-8")
    before = sha256_of(p)
    p.write_text("hello world\n", encoding="utf-8")
    assert sha256_of(p) != before


def test_launch_editor_dry_run_does_not_touch_file(tmp_path: Path):
    p = tmp_path / "x"
    p.write_text("original\n", encoding="utf-8")
    s = Settings(editor="/bin/true")
    result = launch_editor(s, p, dry_run=True)
    assert result.dry_run is True
    assert result.exit_code == 0
    assert p.read_text() == "original\n"


def test_launch_editor_actually_runs_a_no_op_editor(tmp_path: Path):
    """Use /bin/true as the editor — it exits 0 and changes nothing."""
    p = tmp_path / "x"
    p.write_text("hi\n", encoding="utf-8")
    s = Settings(editor="/bin/true")
    result = launch_editor(s, p)
    assert result.dry_run is False
    assert result.exit_code == 0
    assert p.read_text() == "hi\n"
