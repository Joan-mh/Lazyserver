"""GitBackupStore — Plain layout wrapped in a git repo (FR-2.5).

Same on-disk shape as ``PlainBackupStore`` (timestamped copies under
``<root>/<entry_id>/<ts>/...``), with one git commit per backup
*operation* so the student gets free history and diffs. Composition
over inheritance: a Plain store handles the layout, this class adds
``git init`` / ``git add`` / ``git commit`` around it.

Why shell out to ``git`` rather than depend on a library: spec §2 / arch
§1 says minimize dependencies, and arch §3 picks the CLI explicitly. It
also means the student's archive is a normal git repo they can clone,
rebase, push, or inspect with their own tools.

Ownership rule (mirror of PlainBackupStore): lazyserver runs as root,
the student owns the archive. We chown ``.git/**`` to ``target_user``
after every operation that writes to it (init, commit). The configured
``root`` itself is never re-chowned — that pre-existing ownership is
the user's choice.

Root-vs-target_user mismatch + ``safe.directory``: because we run as
root while the repo and .git directory are target_user-owned, modern
git refuses to operate with ``fatal: detected dubious ownership``.
Every invocation goes through ``_git()`` which prepends ``-c
safe.directory=<root>`` so the trust mark is scoped to this one repo —
no global git config is touched.

Failure model:
  * ``git init`` failures during construction *do* raise: if we
    chose Git at startup, an init failure is a real problem the user
    needs to see.
  * ``git commit`` failures in ``commit_operation`` are logged at
    warning level and swallowed. The Plain layout under the working
    tree is already a complete record of the snapshot — losing the
    history layer is an enhancement-only regression, not a data loss.
    Per the BackupStore Protocol contract.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..platform.runner import RunResult, run
from ..platform.user import TargetUser
from .store import FileMetadata, SnapshotRef
from .store_plain import PlainBackupStore

log = logging.getLogger("lazyserver.backup.store_git")

GIT_TIMEOUT_S = 30.0
DEFAULT_AUTHOR_NAME = "lazyserver"
DEFAULT_AUTHOR_EMAIL_DOMAIN = "lazyserver.local"


@dataclass
class GitBackupStore:
    """BackupStore impl: Plain layout + one commit per backup operation."""

    root: Path
    target_user: TargetUser | None = None
    _plain: PlainBackupStore = field(init=False)

    def __post_init__(self) -> None:
        self._plain = PlainBackupStore(root=self.root, target_user=self.target_user)
        self._ensure_repo()

    # ---------- BackupStore protocol ----------

    def snapshot(
        self,
        *,
        entry_id: str,
        source: Path,
        timestamp: str,
        metadata: FileMetadata,
    ) -> SnapshotRef:
        # Layout is identical to Plain. Staging is deferred to
        # commit_operation via `git add -A`, which also catches
        # baselines.json and metadata.json — one source of truth for
        # "what changed".
        return self._plain.snapshot(
            entry_id=entry_id,
            source=source,
            timestamp=timestamp,
            metadata=metadata,
        )

    def list_snapshots(self, entry_id: str) -> list[str]:
        return self._plain.list_snapshots(entry_id)

    def list_files(self, entry_id: str, timestamp: str) -> list[Path]:
        return self._plain.list_files(entry_id, timestamp)

    def read(self, ref: SnapshotRef) -> bytes:
        return self._plain.read(ref)

    def read_metadata(
        self, entry_id: str, timestamp: str
    ) -> dict[Path, FileMetadata] | None:
        return self._plain.read_metadata(entry_id, timestamp)

    def commit_operation(self, *, message: str) -> None:
        try:
            # Plain writes metadata.json first so `git add -A` picks
            # it up alongside content and baselines.json in this one
            # commit. Order matters: a git commit before plain has
            # written would leave metadata.json untracked until the
            # next operation.
            self._plain.commit_operation(message=message)
            status = self._git("status", "--porcelain")
            if not status.stdout.strip():
                log.info("git: nothing to commit in %s", self.root)
                return
            self._git("add", "-A")
            self._git("commit", "-q", "-m", message)
            self._chown_git_dir()
        except Exception as exc:
            log.warning(
                "git commit_operation failed in %s: %s (plain layout intact)",
                self.root,
                exc,
            )

    # ---------- internal ----------

    def _ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").is_dir():
            return
        log.info("git init in backup store at %s", self.root)
        self._git("init", "-q")
        # Repo-local identity so commits work even on a fresh VM where
        # the global ~/.gitconfig is empty.
        name = self.target_user.name if self.target_user else DEFAULT_AUTHOR_NAME
        email = f"{name}@{DEFAULT_AUTHOR_EMAIL_DOMAIN}"
        self._git("config", "user.name", name)
        self._git("config", "user.email", email)
        self._chown_git_dir()

    def _git(self, *args: str) -> RunResult:
        argv = [
            "git",
            "-c",
            f"safe.directory={self.root}",
            "-C",
            str(self.root),
            *args,
        ]
        result = run(argv, timeout=GIT_TIMEOUT_S)
        if not result.ok:
            raise RuntimeError(
                f"git {' '.join(args)} failed (exit {result.exit_code}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def _chown_git_dir(self) -> None:
        if self.target_user is None:
            return
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return
        uid, gid = self.target_user.uid, self.target_user.gid
        for dirpath, dirnames, filenames in os.walk(git_dir):
            self._chown(Path(dirpath), uid, gid)
            for name in dirnames + filenames:
                self._chown(Path(dirpath) / name, uid, gid)

    @staticmethod
    def _chown(path: Path, uid: int, gid: int) -> None:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except (PermissionError, FileNotFoundError) as exc:
            # Survivable: same posture as _fsutil._chown. We log at
            # debug because .git churn produces a lot of paths and a
            # transient miss (e.g. a pack file replaced mid-walk) is
            # not actionable.
            log.debug("chown %d:%d on %s failed: %s", uid, gid, path, exc)
