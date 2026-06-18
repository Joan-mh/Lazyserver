"""Create a new file inside a file_set or a missing fixed file
(FR-1.7 / FR-1.8 / FR-1.9).

The ownership resolution is the testable core: it must produce the same
plan whether the file is actually created or not, so a student can see
the permission preview before committing. The chain is exactly the one
spelled out in FR-1.8:

  service kind:
    1. explicit owner/group/mode on the file_set
    2. copy from an existing sibling file in the set
    3. directory owner/group + default mode 0640
    4. last resort root:root + warning that the service may not read it

  app kind:
    target user owns; mode 0644

The chain is pure logic; reachability of pwd/grp lookups is the only
side effect, so the ResolvedDirectory + TargetUser inputs make it
deterministic for tests.
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
from dataclasses import dataclass
from pathlib import Path

from ..platform.user import TargetUser
from ..tconf.model import KIND_APP, KIND_SERVICE

log = logging.getLogger("lazyserver.backup.create")

SERVICE_DEFAULT_MODE = "0640"
APP_DEFAULT_MODE = "0644"
ROOT_OWNER = "root"
ROOT_GROUP = "root"


class CreateError(RuntimeError):
    """A create operation cannot proceed (e.g. target already exists)."""


@dataclass(frozen=True)
class OwnershipPlan:
    """How a newly-created file should be owned and permissioned."""

    owner: str
    group: str
    mode: str  # octal string, e.g. "0640"
    reason: str  # human-readable: why this was chosen
    is_fallback_root: bool = False  # True for the root:root warning case


def plan_ownership(
    *,
    entry_kind: str,
    directory: Path,
    target_user: TargetUser,
    explicit_owner: str | None = None,
    explicit_group: str | None = None,
    explicit_mode: str | None = None,
) -> OwnershipPlan:
    """Resolve ownership for a new file (FR-1.8).

    `entry_kind` is `service` or `app`. For `app`, the target user owns
    unconditionally — the explicit_* args (which mirror the file_set
    override fields on services) are ignored to keep app config in home
    private to the user.
    """
    if entry_kind == KIND_APP:
        return _plan_app(target_user)
    if entry_kind == KIND_SERVICE:
        return _plan_service(
            directory=directory,
            explicit_owner=explicit_owner,
            explicit_group=explicit_group,
            explicit_mode=explicit_mode,
        )
    raise ValueError(f"Unknown entry kind {entry_kind!r}")


def create_file(
    path: Path,
    *,
    content: str,
    plan: OwnershipPlan,
    dry_run: bool = False,
) -> None:
    """Write `content` to `path` and apply the ownership plan.

    Refuses to overwrite an existing file (FR-1.7). Dry-run skips both
    the write and the chown/chmod so the flow is testable without root.
    """
    if path.exists():
        raise CreateError(f"refuse to overwrite existing file {path}")
    if dry_run:
        log.info("dry-run: would create %s with plan %s", path, plan)
        return
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, int(plan.mode, 8))
    except PermissionError:
        log.warning("chmod %s on %s failed (insufficient privilege)", plan.mode, path)
    try:
        uid = pwd.getpwnam(plan.owner).pw_uid
        gid = grp.getgrnam(plan.group).gr_gid
        os.chown(path, uid, gid)
    except (KeyError, PermissionError) as exc:
        log.warning("chown %s:%s on %s failed: %s", plan.owner, plan.group, path, exc)


# ---------- chain implementations ----------


def _plan_app(target_user: TargetUser) -> OwnershipPlan:
    group_name = _gid_to_name(target_user.gid) or target_user.name
    return OwnershipPlan(
        owner=target_user.name,
        group=group_name,
        mode=APP_DEFAULT_MODE,
        reason=f"app entry — owned by target user {target_user.name}",
    )


def _plan_service(
    *,
    directory: Path,
    explicit_owner: str | None,
    explicit_group: str | None,
    explicit_mode: str | None,
) -> OwnershipPlan:
    # Step 1 — explicit override on the file_set.
    if explicit_owner or explicit_group or explicit_mode:
        return OwnershipPlan(
            owner=explicit_owner or _root_or_dir_owner(directory) or ROOT_OWNER,
            group=explicit_group or _root_or_dir_group(directory) or ROOT_GROUP,
            mode=explicit_mode or SERVICE_DEFAULT_MODE,
            reason="file_set explicit owner/group/mode override",
        )

    # Step 2 — copy from an existing sibling.
    sibling = _pick_sibling(directory)
    if sibling is not None:
        owner = _uid_to_name(sibling.stat().st_uid) or ROOT_OWNER
        group = _gid_to_name(sibling.stat().st_gid) or ROOT_GROUP
        mode = _stat_mode_to_octal(sibling.stat().st_mode)
        return OwnershipPlan(
            owner=owner,
            group=group,
            mode=mode,
            reason=f"copied from sibling file {sibling}",
        )

    # Step 3 — directory owner/group + default mode.
    if directory.is_dir():
        st = directory.stat()
        owner = _uid_to_name(st.st_uid)
        group = _gid_to_name(st.st_gid)
        if owner and group:
            return OwnershipPlan(
                owner=owner,
                group=group,
                mode=SERVICE_DEFAULT_MODE,
                reason=f"matched from directory {directory} owner",
            )

    # Step 4 — last resort.
    return OwnershipPlan(
        owner=ROOT_OWNER,
        group=ROOT_GROUP,
        mode=SERVICE_DEFAULT_MODE,
        reason=(
            f"no siblings in {directory} and directory owner unresolved; "
            "falling back to root:root — the service may be unable to read this file"
        ),
        is_fallback_root=True,
    )


def _pick_sibling(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    for entry in sorted(directory.iterdir()):
        if entry.is_file():
            return entry
    return None


def _uid_to_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _gid_to_name(gid: int) -> str | None:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def _root_or_dir_owner(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    return _uid_to_name(directory.stat().st_uid)


def _root_or_dir_group(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    return _gid_to_name(directory.stat().st_gid)


def _stat_mode_to_octal(st_mode: int) -> str:
    return f"{st_mode & 0o7777:04o}"
