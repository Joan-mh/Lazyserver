"""FR-1.6 — file_set glob expansion is fresh on every call.

Lives in tconf.resolve so both the TUI and backup.pending share one
implementation. Tests assert: live filesystem state drives the result,
non-files are filtered out, and `**` is honored only when present in
the pattern.
"""

from __future__ import annotations

from pathlib import Path

from lazyserver.tconf.resolve import ResolvedFileSet, expand_file_set


def _set(directory: Path, pattern: str = "db.*") -> ResolvedFileSet:
    return ResolvedFileSet(
        id="zone_files",
        directory=str(directory),
        pattern=pattern,
        description="zone files",
        example=None,
        optional=True,
        owner=None,
        group=None,
        mode=None,
    )


def test_returns_empty_for_missing_directory(tmp_path: Path):
    fs = _set(tmp_path / "nope")
    assert expand_file_set(fs) == []


def test_lists_matching_files_sorted(tmp_path: Path):
    (tmp_path / "db.z").write_text("z")
    (tmp_path / "db.a").write_text("a")
    (tmp_path / "other.conf").write_text("nope")
    fs = _set(tmp_path)
    assert expand_file_set(fs) == [tmp_path / "db.a", tmp_path / "db.z"]


def test_filters_out_directories(tmp_path: Path):
    (tmp_path / "db.real").write_text("x")
    (tmp_path / "db.dir").mkdir()  # matches the glob but is a directory
    fs = _set(tmp_path)
    assert expand_file_set(fs) == [tmp_path / "db.real"]


def test_recursive_only_when_pattern_has_double_star(tmp_path: Path):
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "db.deep").write_text("x")
    (tmp_path / "db.top").write_text("y")

    flat = _set(tmp_path)
    assert tmp_path / "db.top" in expand_file_set(flat)
    # Without `**`, nested files are NOT walked.
    assert nested / "db.deep" not in expand_file_set(flat)

    recursive = _set(tmp_path, pattern="**/db.*")
    paths = expand_file_set(recursive)
    assert nested / "db.deep" in paths


def test_files_created_after_initial_call_are_picked_up(tmp_path: Path):
    """The live-VM bug class: glob runs on every call, no caching."""
    fs = _set(tmp_path)
    assert expand_file_set(fs) == []
    (tmp_path / "db.new").write_text("x")
    assert expand_file_set(fs) == [tmp_path / "db.new"]
