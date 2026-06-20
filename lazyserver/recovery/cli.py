"""Recovery CLI dispatcher (FR-5.3 / Phase 6).

One mode for now: ``lsrv recover --all`` rebuilds the system from
tconf + backups. Per-entry recovery (a future ``--entry ID``) is left
unimplemented because the disaster-recovery flow that motivates
Phase 6 is whole-system; piecemeal recovery is what plain
``restore --entry`` already covers.

The dispatcher:

  1. Bootstraps the app (NFR-3 root check, settings, tconf, distro).
  2. Resolves every loadable entry for this distro (skips ones that
     fail to resolve, same posture as the backup/restore CLIs).
  3. Builds a ``RecoveryPlan`` and runs ``execute_recovery``.
  4. Writes both artifacts under ``<store>/recovery/recovery-TS.{log,json}``
     with target-user ownership.
  5. Prints the human log to stdout so the operator sees the run
     live; the artifact paths are echoed at the end.

**Exit codes** mirror backup/restore so the disaster-recovery script
can branch on them deterministically:

  * 0 — every entry rolled up to ``ok`` (or ``would_run`` for ``--dry-run``)
  * 1 — hard precondition fail (no store, no entries, bootstrap error)
  * 2 — at least one entry rolled up to ``partial`` or ``failed``

Dry-run still produces both artifacts (``"dry_run": true`` in the JSON,
``[DRY RUN]`` in the log header) and exits 0 — the plan is a useful
script-readable preview, not a result.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from ..app import BootstrapError, bootstrap
from ..backup.pending import BaselineStore
from ..backup.run import current_timestamp
from ..backup.store import make_backup_store
from ..tconf.resolve import ResolutionError, ResolvedEntry, resolve
from .plan import build_recovery_plan
from .report import (
    ENTRY_FAILED,
    ENTRY_PARTIAL,
    format_human_log,
    summarise,
    write_recovery_artifacts,
)
from .run import execute_recovery

log = logging.getLogger("lazyserver.recovery.cli")

EXIT_OK = 0
EXIT_HARD_ERROR = 1
EXIT_PARTIAL_FAILURE = 2


def cmd_recover(
    *,
    all_entries: bool,
    store_override: Path | None,
    dry_run: bool,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Argparse-friendly entry point for ``lsrv recover --all``."""
    if not all_entries:
        # `--all` is currently required to make whole-system intent
        # explicit; argparse marks it optional so we can give a clear
        # message instead of usage noise.
        print(
            "lazyserver: recover requires --all "
            "(per-entry recovery is covered by `lsrv restore --entry ID`).",
            file=err,
        )
        return EXIT_HARD_ERROR

    try:
        context = bootstrap()
    except BootstrapError as exc:
        print(f"lazyserver: {exc}", file=err)
        return EXIT_HARD_ERROR

    # Resolve store (override → settings).
    from ..backup.restore_cli import _ConfigError, _resolve_store_path

    try:
        store_path = _resolve_store_path(
            context.settings, store_override, context.target_user
        )
    except _ConfigError as exc:
        print(f"lazyserver: {exc}", file=err)
        return EXIT_HARD_ERROR

    resolved_map = _resolve_all_entries(context)
    if not resolved_map:
        print(
            f"lazyserver: no entries resolved for distro "
            f"{context.distro.id!r} — nothing to recover.",
            file=err,
        )
        return EXIT_HARD_ERROR

    store = make_backup_store(store_path, target_user=context.target_user)
    plan = build_recovery_plan(
        resolved_entries=resolved_map.values(),
        store=store,
        distro_id=context.distro.id,
    )

    timestamp = current_timestamp()
    baselines = BaselineStore.load(store_path, target_user=context.target_user)

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=context.target_user,
        timestamp=timestamp,
        dry_run=dry_run,
    )

    # Stream the human log to stdout so the operator does not have to
    # cat the artifact to know what happened. The trailing newline is
    # already in format_human_log's output.
    print(format_human_log(report), end="", file=out)

    # Write both artifacts. The TUI shares write_recovery_artifacts so
    # both surfaces produce byte-identical files for the same report.
    log_path, json_path = write_recovery_artifacts(
        report, store_root=store_path, target_user=context.target_user
    )

    print(file=out)
    print(f"Artifacts written:", file=out)
    print(f"  log:  {log_path}", file=out)
    print(f"  json: {json_path}", file=out)

    summary = summarise(report.entries)
    if summary[ENTRY_FAILED] + summary[ENTRY_PARTIAL] > 0:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def _resolve_all_entries(context) -> dict[str, ResolvedEntry]:
    """Resolve every entry; drop the ones that fail on this distro.

    Mirrors the backup/restore CLIs' posture: one unresolvable entry
    (e.g. a service that doesn't ship on this distro) should not block
    the rest of the recovery.
    """
    resolved: dict[str, ResolvedEntry] = {}
    for entry in context.entries:
        try:
            resolved[entry.id] = resolve(
                entry, context.distro.id, target_user=context.target_user
            )
        except ResolutionError as exc:
            log.warning("skip %s: %s", entry.id, exc)
    return resolved
