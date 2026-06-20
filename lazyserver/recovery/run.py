"""Recovery orchestrator — wire plan → install → restore → enable (FR-5.3).

Consumes a ``RecoveryPlan`` produced by :mod:`recovery.plan` and a
matching ``{entry_id: ResolvedEntry}`` map; produces a fully populated
``RecoveryReport`` consumed by :mod:`recovery.report` formatters.

**Cascade rule** (per the design Joan signed off on):

  * ``install`` fails for an entry → ``restore`` AND ``enable`` are
    marked SKIPPED with reason "install failed". Writing config onto a
    half-installed package, or enabling a unit that does not exist,
    only generates noise that hides the real root cause.
  * ``restore`` fails → ``enable`` still runs. A running service on
    stock config beats a stopped service; the failure is loud in both
    artifacts and the student can fix and ``reload`` interactively.
  * ``enable`` fails → install + restore stand; entry rolls up to
    ``partial`` via :func:`recovery.report.derive_entry_status`.

**Cross-entry**: each entry is independent. One entry's failure never
blocks another. Iteration order = plan order = alphabetical by id.

**Pre-restore snapshots are skipped during recovery** (Joan's decision
2): the orchestrator passes ``take_pre_restore=False`` to
:func:`backup.restore.execute_restore`. On a freshly installed box
the live file is the stock vendor copy the package manager just laid
down; snapshotting it as a pre-restore handle would pollute the store
with vendor bytes.

**Dry-run** is fully supported: every applicable step becomes a
WOULD-RUN result with the argv (install / enable) or snapshot +
file_count (restore) already populated. No subprocess is spawned, no
file is touched, no baseline is updated — the orchestrator simply
walks the plan and synthesises the StepResults.

This module owns no I/O of its own outside what its callees do; path
creation and artifact writing are the CLI / TUI's job (step 5+).
"""

from __future__ import annotations

import logging

from ..backup.pending import BaselineStore
from ..backup.restore import (
    RestoreOutcome,
    RestoreSelection,
    SnapshotChoice,
    execute_restore,
    plan_restore,
)
from ..backup.store import BackupStore
from ..platform.runner import RunResult
from ..platform.user import TargetUser
from ..services.control import (
    NoInstallCommandError,
    UnsupportedActionError,
    execute_action,
    execute_install,
)
from ..tconf.resolve import ResolvedEntry
from .plan import EntryPlan, RecoveryPlan
from .report import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WOULD_RUN,
    EntryResult,
    RecoveryReport,
    StepResult,
    derive_entry_status,
    tail_stderr,
)

log = logging.getLogger("lazyserver.recovery.run")

CASCADE_REASON = "install failed"


def execute_recovery(
    plan: RecoveryPlan,
    *,
    resolved_entries: dict[str, ResolvedEntry],
    store: BackupStore,
    baselines: BaselineStore,
    target_user: TargetUser,
    timestamp: str,
    dry_run: bool = False,
) -> RecoveryReport:
    """Run the plan end to end, returning a populated RecoveryReport.

    ``timestamp`` is the recovery operation id — used both as the
    pre-restore TS handed down to ``execute_restore`` (a no-op when
    ``take_pre_restore=False`` but still threaded through for logging)
    and as the artifact-filename suffix (the caller turns it into
    ``recovery-<timestamp>.{log,json}``).
    """
    entry_results: list[EntryResult] = []
    for entry_plan in plan.entries:
        resolved = resolved_entries.get(entry_plan.entry_id)
        if resolved is None:
            # Defensive: a plan built from these resolved entries cannot
            # reach this branch. If it does, mark every step skipped
            # rather than crash mid-recovery (FR-5.3.2: "everything done").
            steps = (
                StepResult(
                    name="install",
                    status=STATUS_SKIPPED,
                    reason="entry not in resolved_entries map",
                ),
                StepResult(
                    name="restore",
                    status=STATUS_SKIPPED,
                    reason="entry not in resolved_entries map",
                ),
                StepResult(
                    name="enable",
                    status=STATUS_SKIPPED,
                    reason="entry not in resolved_entries map",
                ),
            )
        else:
            install_step = _run_install_step(entry_plan, resolved, dry_run=dry_run)
            cascade = install_step.status == STATUS_FAILED
            restore_step = _run_restore_step(
                entry_plan,
                resolved,
                store=store,
                baselines=baselines,
                target_user=target_user,
                pre_restore_timestamp=timestamp,
                dry_run=dry_run,
                cascade_skip=cascade,
            )
            enable_step = _run_enable_step(
                entry_plan,
                resolved,
                dry_run=dry_run,
                cascade_skip=cascade,
            )
            steps = (install_step, restore_step, enable_step)
        entry_results.append(
            EntryResult(
                entry_id=entry_plan.entry_id,
                entry_name=entry_plan.entry_name,
                is_service=entry_plan.is_service,
                status=derive_entry_status(steps),
                steps=steps,
            )
        )
    return RecoveryReport(
        timestamp=timestamp,
        distro_id=plan.distro_id,
        ordering_note=plan.ordering_note,
        dry_run=dry_run,
        entries=tuple(entry_results),
    )


# ---------- per-step runners ----------


def _run_install_step(
    entry_plan: EntryPlan, resolved: ResolvedEntry, *, dry_run: bool
) -> StepResult:
    step_plan = entry_plan.install
    if not step_plan.applicable:
        return StepResult(
            name="install",
            status=STATUS_SKIPPED,
            reason=step_plan.reason_skipped,
        )
    if dry_run:
        return StepResult(
            name="install",
            status=STATUS_WOULD_RUN,
            argv=step_plan.argv,
        )
    try:
        result = execute_install(resolved)
    except NoInstallCommandError as exc:
        # Should be unreachable — planner gates this — but be defensive.
        log.warning("install raised NoInstallCommandError for %s: %s", entry_plan.entry_id, exc)
        return StepResult(
            name="install",
            status=STATUS_FAILED,
            argv=step_plan.argv,
            stderr_tail=str(exc),
        )
    except Exception as exc:
        log.warning(
            "install crashed for %s (argv=%s): %s",
            entry_plan.entry_id, step_plan.argv, exc,
        )
        return StepResult(
            name="install",
            status=STATUS_FAILED,
            argv=step_plan.argv,
            stderr_tail=str(exc),
        )
    return _runresult_to_step("install", result)


def _run_restore_step(
    entry_plan: EntryPlan,
    resolved: ResolvedEntry,
    *,
    store: BackupStore,
    baselines: BaselineStore,
    target_user: TargetUser,
    pre_restore_timestamp: str,
    dry_run: bool,
    cascade_skip: bool,
) -> StepResult:
    step_plan = entry_plan.restore
    if cascade_skip:
        return StepResult(name="restore", status=STATUS_SKIPPED, reason=CASCADE_REASON)
    if not step_plan.applicable:
        return StepResult(
            name="restore",
            status=STATUS_SKIPPED,
            reason=step_plan.reason_skipped,
        )
    if dry_run:
        return StepResult(
            name="restore",
            status=STATUS_WOULD_RUN,
            snapshot=step_plan.snapshot,
            files_restored=step_plan.file_count,
        )

    resolved_map = {entry_plan.entry_id: resolved}
    try:
        restore_plan = plan_restore(
            selection=RestoreSelection(
                entry_ids=(entry_plan.entry_id,),
                file_paths=None,
                snapshot_choice=SnapshotChoice(
                    timestamps={entry_plan.entry_id: step_plan.snapshot}
                ),
            ),
            resolved_entries=resolved_map,
            store=store,
        )
        reports = execute_restore(
            restore_plan,
            store=store,
            baselines=baselines,
            resolved_entries=resolved_map,
            target_user=target_user,
            pre_restore_timestamp=pre_restore_timestamp,
            take_pre_restore=False,
        )
    except Exception as exc:
        log.warning(
            "restore crashed for %s (snapshot=%s): %s",
            entry_plan.entry_id, step_plan.snapshot, exc,
        )
        return StepResult(
            name="restore",
            status=STATUS_FAILED,
            snapshot=step_plan.snapshot,
            stderr_tail=str(exc),
        )

    restored = sum(1 for r in reports if r.outcome is RestoreOutcome.RESTORED)
    failed = sum(
        1
        for r in reports
        if r.outcome
        in (RestoreOutcome.WRITE_FAILED, RestoreOutcome.PRE_SNAPSHOT_FAILED)
    )
    extras = sum(1 for r in reports if r.outcome is RestoreOutcome.EXTRA_REPORTED)
    status = STATUS_OK if failed == 0 else STATUS_FAILED
    return StepResult(
        name="restore",
        status=status,
        snapshot=step_plan.snapshot,
        files_restored=restored,
        files_failed=failed,
        extras_reported=extras,
        stderr_tail=_format_restore_failures(reports) if failed else None,
    )


def _run_enable_step(
    entry_plan: EntryPlan,
    resolved: ResolvedEntry,
    *,
    dry_run: bool,
    cascade_skip: bool,
) -> StepResult:
    step_plan = entry_plan.enable
    if cascade_skip:
        return StepResult(name="enable", status=STATUS_SKIPPED, reason=CASCADE_REASON)
    if not step_plan.applicable:
        return StepResult(
            name="enable",
            status=STATUS_SKIPPED,
            reason=step_plan.reason_skipped,
        )
    if dry_run:
        return StepResult(
            name="enable",
            status=STATUS_WOULD_RUN,
            argv=step_plan.argv,
        )
    try:
        result = execute_action(resolved, "enable_now")
    except UnsupportedActionError as exc:
        log.warning("enable raised UnsupportedActionError for %s: %s", entry_plan.entry_id, exc)
        return StepResult(
            name="enable",
            status=STATUS_FAILED,
            argv=step_plan.argv,
            stderr_tail=str(exc),
        )
    except Exception as exc:
        log.warning(
            "enable crashed for %s (argv=%s): %s",
            entry_plan.entry_id, step_plan.argv, exc,
        )
        return StepResult(
            name="enable",
            status=STATUS_FAILED,
            argv=step_plan.argv,
            stderr_tail=str(exc),
        )
    return _runresult_to_step("enable", result)


# ---------- result mapping ----------


def _runresult_to_step(name: str, result: RunResult) -> StepResult:
    """Map a RunResult into a StepResult for install / enable steps."""
    if result.ok:
        return StepResult(
            name=name,
            status=STATUS_OK,
            argv=result.argv,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
        )
    return StepResult(
        name=name,
        status=STATUS_FAILED,
        argv=result.argv,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        stderr_tail=tail_stderr(result.stderr),
    )


def _format_restore_failures(reports) -> str | None:
    """Compact summary of per-file restore failures for the step's
    stderr_tail. One line per failure: outcome + path + error head."""
    lines: list[str] = []
    for r in reports:
        if r.outcome is RestoreOutcome.WRITE_FAILED and r.item:
            lines.append(f"WRITE_FAILED {r.item.source_path}: {r.error or 'unknown'}")
        elif r.outcome is RestoreOutcome.PRE_SNAPSHOT_FAILED and r.item:
            lines.append(
                f"PRE_SNAPSHOT_FAILED {r.item.source_path}: {r.error or 'unknown'}"
            )
    return "\n".join(lines) if lines else None
