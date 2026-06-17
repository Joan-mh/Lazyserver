"""Target-user resolution (FR-1.10).

LazyServer typically runs under `sudo`/root, but the real human is identified
by the target user. Resolution order: settings override → $SUDO_USER → $USER.
Fails loudly if pwd lookup fails (NFR-3, NFR-5).
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path


class TargetUserError(RuntimeError):
    """Raised when no target user can be resolved or looked up."""


@dataclass(frozen=True)
class TargetUser:
    name: str
    uid: int
    gid: int
    home: Path


def resolve(
    *,
    override: str | None = None,
    env: dict[str, str] | None = None,
) -> TargetUser:
    """Resolve the target user.

    Order: explicit override (FR-7.1 setting) → $SUDO_USER → $USER.
    Raises TargetUserError if none is set or the chosen name has no pwd entry.
    """
    environ = env if env is not None else os.environ
    candidates = [override, environ.get("SUDO_USER"), environ.get("USER")]
    name = next((c for c in candidates if c), None)
    if not name:
        raise TargetUserError(
            "Cannot resolve target user: no override, $SUDO_USER, or $USER set."
        )
    try:
        entry = pwd.getpwnam(name)
    except KeyError as exc:
        raise TargetUserError(
            f"Target user {name!r} has no /etc/passwd entry."
        ) from exc
    return TargetUser(
        name=entry.pw_name,
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        home=Path(entry.pw_dir),
    )
