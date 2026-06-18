"""Ownership planning + file creation for FR-1.7/8/9."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup.create import (
    APP_DEFAULT_MODE,
    SERVICE_DEFAULT_MODE,
    CreateError,
    OwnershipPlan,
    create_file,
    plan_ownership,
)
from lazyserver.platform.user import TargetUser


def _self() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def test_app_plan_is_target_user(tmp_path: Path):
    user = _self()
    plan = plan_ownership(
        entry_kind="app",
        directory=tmp_path,
        target_user=user,
    )
    assert plan.owner == user.name
    assert plan.mode == APP_DEFAULT_MODE
    assert "target user" in plan.reason
    assert plan.is_fallback_root is False


def test_app_plan_ignores_explicit_overrides(tmp_path: Path):
    """App entries always belong to the user, regardless of overrides."""
    user = _self()
    plan = plan_ownership(
        entry_kind="app",
        directory=tmp_path,
        target_user=user,
        explicit_owner="root",
        explicit_group="root",
        explicit_mode="0600",
    )
    assert plan.owner == user.name
    assert plan.mode == APP_DEFAULT_MODE


def test_service_plan_uses_explicit_override(tmp_path: Path):
    user = _self()
    (tmp_path / "sibling").write_bytes(b"x")  # would otherwise match step 2
    plan = plan_ownership(
        entry_kind="service",
        directory=tmp_path,
        target_user=user,
        explicit_owner="bind",
        explicit_group="bind",
        explicit_mode="0600",
    )
    assert plan.owner == "bind"
    assert plan.group == "bind"
    assert plan.mode == "0600"
    assert "override" in plan.reason


def test_service_plan_copies_from_sibling(tmp_path: Path):
    """An existing sibling file is the canonical source of truth."""
    user = _self()
    sibling = tmp_path / "db.example"
    sibling.write_text("x", encoding="utf-8")
    os.chmod(sibling, 0o644)  # known mode
    plan = plan_ownership(
        entry_kind="service",
        directory=tmp_path,
        target_user=user,
    )
    assert plan.owner == user.name  # whoever created the sibling
    assert plan.mode == "0644"
    assert "sibling" in plan.reason
    assert plan.is_fallback_root is False


def test_service_plan_uses_directory_owner_when_empty(tmp_path: Path):
    user = _self()
    plan = plan_ownership(
        entry_kind="service",
        directory=tmp_path,
        target_user=user,
    )
    # tmp_path is owned by the test user; no siblings.
    assert plan.owner == user.name
    assert plan.mode == SERVICE_DEFAULT_MODE
    assert "directory" in plan.reason
    assert plan.is_fallback_root is False


def test_service_plan_falls_back_to_root_for_missing_directory(tmp_path: Path):
    user = _self()
    missing = tmp_path / "does-not-exist"
    plan = plan_ownership(
        entry_kind="service",
        directory=missing,
        target_user=user,
    )
    assert plan.owner == "root"
    assert plan.group == "root"
    assert plan.is_fallback_root is True
    assert "service may be unable to read" in plan.reason


def test_create_file_writes_content_and_mode(tmp_path: Path):
    user = _self()
    plan = OwnershipPlan(
        owner=user.name,
        group=pwd.getpwuid(user.gid).pw_name if user.gid == user.uid else _self_group(),
        mode="0640",
        reason="test",
    )
    path = tmp_path / "new.conf"
    create_file(path, content="hello\n", plan=plan)
    assert path.read_text() == "hello\n"
    # Mode actually applied.
    assert oct(path.stat().st_mode & 0o7777) == "0o640"


def _self_group() -> str:
    import grp
    return grp.getgrgid(os.getgid()).gr_name


def test_create_file_refuses_overwrite(tmp_path: Path):
    user = _self()
    plan = plan_ownership(entry_kind="app", directory=tmp_path, target_user=user)
    path = tmp_path / "exists"
    path.write_text("untouched\n", encoding="utf-8")
    with pytest.raises(CreateError, match="refuse to overwrite"):
        create_file(path, content="new\n", plan=plan)
    assert path.read_text() == "untouched\n"


def test_create_file_dry_run_does_not_touch_disk(tmp_path: Path):
    user = _self()
    plan = plan_ownership(entry_kind="app", directory=tmp_path, target_user=user)
    path = tmp_path / "would-create"
    create_file(path, content="hello\n", plan=plan, dry_run=True)
    assert not path.exists()
