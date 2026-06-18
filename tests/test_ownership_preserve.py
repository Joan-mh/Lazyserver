"""FR-1.11 — owner/group/mode survive an edit.

We can't change uid without root, so the uid-preservation half is
verified by simulating an editor that rewrites the file with a
different mode and asserting reapply puts the mode back. The chown
path is logic-checked by calling reapply against a snapshot that
matches the current owner (no-op success).
"""

from __future__ import annotations

import os
from pathlib import Path

from lazyserver.backup.ownership import reapply, snapshot


def test_snapshot_returns_none_for_missing_file(tmp_path: Path):
    assert snapshot(tmp_path / "nope") is None


def test_snapshot_captures_current_mode(tmp_path: Path):
    p = tmp_path / "f"
    p.write_text("x")
    os.chmod(p, 0o600)
    snap = snapshot(p)
    assert snap is not None
    assert snap.mode == 0o600


def test_reapply_restores_mode_after_rewrite(tmp_path: Path):
    """Simulates an editor that saves via temp-file rename: the
    original file is replaced and inherits a different mode."""
    p = tmp_path / "f"
    p.write_text("before")
    os.chmod(p, 0o600)
    snap = snapshot(p)

    # Simulate the rewrite-and-rename: new file, default mode.
    p.unlink()
    p.write_text("after")
    os.chmod(p, 0o644)
    assert p.stat().st_mode & 0o7777 == 0o644

    changed = reapply(p, snap)
    assert changed is True
    assert p.stat().st_mode & 0o7777 == 0o600
    # Content must be untouched.
    assert p.read_text() == "after"


def test_reapply_is_noop_when_nothing_changed(tmp_path: Path):
    p = tmp_path / "f"
    p.write_text("x")
    os.chmod(p, 0o640)
    snap = snapshot(p)
    assert reapply(p, snap) is False
    assert p.stat().st_mode & 0o7777 == 0o640


def test_reapply_silent_when_target_disappeared(tmp_path: Path):
    p = tmp_path / "f"
    p.write_text("x")
    snap = snapshot(p)
    p.unlink()
    # Should not raise even though the file is gone.
    assert reapply(p, snap) is False


def test_reapply_with_none_snapshot_is_noop(tmp_path: Path):
    p = tmp_path / "f"
    p.write_text("x")
    os.chmod(p, 0o644)
    # snap=None means "the file did not exist before"; the create flow
    # owns initial ownership in that case.
    assert reapply(p, None) is False
    assert p.stat().st_mode & 0o7777 == 0o644
