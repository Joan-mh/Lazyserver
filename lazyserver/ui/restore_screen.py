"""Restore TUI — entry → snapshots → files → R (FR-3, Phase 5).

Two stacked screens, one entry point:

  * :class:`RestoreSnapshotsScreen` — opens from HomeScreen / EntryScreen
    when an entry is focused. Lists that entry's snapshots oldest →
    newest with pre-restore snapshots flagged. Enter drills into a
    snapshot.

  * :class:`RestoreFilesScreen` — shows what the chosen snapshot would
    restore: each file with the resolved (uid/gid/mode) inline so
    ``bind:bind 0640`` is verifiable *before* the action, plus file_set
    extras flagged ``EXT  not touched`` per FR-3.4. Uppercase ``R``
    runs the restore (no confirmation per NFR-2 + §9 — the finger-gate
    mirrors backup's ``B``).

**Why the two levels.** Restore's defining decision is the moment to
rewind to. Flattening that to "latest" would hide the heart of the
flow and turn a one-keystroke action into a foot-gun. The level split
makes the timestamp choice deliberate and visible.

**Post-action surface.** A modal mirrors the CLI's loud copy-paste
undo banner: the pre-restore timestamp plus the literal
``lsrv restore --entry ID --snapshot <ts>-pre-restore`` line. That
banner is FR-3.2's safety net made visible — same shape inside the
TUI as on the command line, so VM verification of the undo round-trip
works identically through either surface.

Navigation lives on the source screens (``r`` on HomeScreen and
EntryScreen), not on ``LazyServerApp.BINDINGS``. Restore stays behind
explicit per-entry navigation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..backup.pending import BaselineStore
from ..backup.restore import (
    FileSetExtra,
    OwnershipChoice,
    PRE_RESTORE_SUFFIX,
    RestoreItem,
    RestoreOutcome,
    RestorePlan,
    RestoreReport,
    RestoreSelection,
    SnapshotChoice,
    execute_restore,
    plan_restore,
    resolve_ownership,
)
from ..backup.run import current_timestamp
from ..backup.store import BackupStore, make_backup_store
from ..config import resolved_backup_store
from ..tconf.model import KIND_SERVICE
from ..tconf.resolve import ResolutionError, ResolvedEntry, ResolvedFileSet, resolve

log = logging.getLogger("lazyserver.ui.restore_screen")


# ---------- pure helpers (unit-testable without Textual) ----------


@dataclass(frozen=True)
class SnapshotRow:
    """One row on the snapshots screen.

    ``is_pre_restore`` marks snapshots written by a previous restore
    operation so the student can see — and pick — the undo handle
    without parsing the timestamp suffix by eye.
    """

    timestamp: str
    file_count: int
    is_pre_restore: bool

    @property
    def label(self) -> str:
        plural = "" if self.file_count == 1 else "s"
        tag = "  [PRE-RESTORE]" if self.is_pre_restore else ""
        return f"{self.timestamp}    {self.file_count} file{plural}{tag}"


def list_snapshot_rows(store: BackupStore, entry_id: str) -> list[SnapshotRow]:
    """Read the snapshot history for one entry, oldest → newest.

    Pre-restore snapshots are listed alongside regular ones — they are
    the user's undo handles and the screen has to surface them. The
    ``-pre-restore`` suffix is the structural signal we sort on; we
    don't strip it for display because the timestamp the user copies
    into ``--snapshot`` must include the suffix.
    """
    rows: list[SnapshotRow] = []
    for ts in store.list_snapshots(entry_id):
        file_count = len(store.list_files(entry_id, ts))
        rows.append(
            SnapshotRow(
                timestamp=ts,
                file_count=file_count,
                is_pre_restore=ts.endswith(PRE_RESTORE_SUFFIX),
            )
        )
    return rows


@dataclass(frozen=True)
class PlannedRow:
    """One row on the files screen, pre-computed with ownership.

    Ownership is resolved at planning time (not just at execute time)
    so the student can see ``uid=121 gid=127 mode=0o640`` *before*
    pressing R — the exact bytes the executor will apply.
    """

    item: RestoreItem
    ownership: OwnershipChoice

    @property
    def label(self) -> str:
        mode = oct(self.ownership.mode)
        warn = "  ⚠" if self.ownership.warnings else ""
        return (
            f"RST  {self.item.source_path}    "
            f"uid={self.ownership.uid} gid={self.ownership.gid} mode={mode}{warn}"
        )


@dataclass(frozen=True)
class ExtraRow:
    """One row for a FR-3.4 extra — clearly flagged 'not touched'."""

    extra: FileSetExtra

    @property
    def label(self) -> str:
        return f"EXT  {self.extra.path}    not touched (FR-3.4)"


def build_planned_rows(
    plan: RestorePlan,
    *,
    resolved_entries: dict[str, ResolvedEntry],
    target_user,
) -> tuple[list[PlannedRow], list[ExtraRow]]:
    """Pre-resolve ownership for every planned item.

    Mirrors what ``execute_restore`` will do per-item so the on-screen
    preview matches the live action. Shared by the screen and by the
    tests so the "preview matches reality" invariant has one source.
    """
    planned: list[PlannedRow] = []
    for item in plan.items:
        resolved = resolved_entries.get(item.entry_id)
        entry_kind = resolved.entry.kind if resolved else KIND_SERVICE
        file_set = _matching_file_set(resolved, item.set_id) if resolved else None
        ownership = resolve_ownership(
            captured=item.captured_metadata,
            live_path=item.source_path,
            entry_kind=entry_kind,
            target_user=target_user,
            file_set=file_set,
        )
        planned.append(PlannedRow(item=item, ownership=ownership))
    extras = [ExtraRow(extra=e) for e in plan.extras]
    return planned, extras


def _matching_file_set(
    resolved: ResolvedEntry | None, set_id: str | None
) -> ResolvedFileSet | None:
    if resolved is None or set_id is None:
        return None
    for fs in resolved.file_sets:
        if fs.id == set_id:
            return fs
    return None


def format_undo_banner(
    pre_restore_ts_full: str, entry_ids: Iterable[str]
) -> list[str]:
    """Lines for the post-action result modal.

    Returns the literal CLI invocations the user can copy. Same shape
    as the CLI's banner so the verification surface is identical.
    """
    lines = [
        f"Pre-restore snapshot: {pre_restore_ts_full}",
        "To undo this restore, run:",
    ]
    for eid in entry_ids:
        lines.append(
            f"    lsrv restore --entry {eid} --snapshot {pre_restore_ts_full}"
        )
    return lines


# ---------- row widgets ----------


class _SnapshotRowItem(ListItem):
    def __init__(self, row: SnapshotRow):
        super().__init__(Label(row.label))
        self.row = row


class _PlannedRowItem(ListItem):
    def __init__(self, row: PlannedRow):
        super().__init__(Label(row.label))
        self.row = row


class _ExtraRowItem(ListItem):
    def __init__(self, row: ExtraRow):
        super().__init__(Label(row.label))
        self.row = row


# ---------- snapshots screen (level 1) ----------


class RestoreSnapshotsScreen(Screen):
    """Pick which snapshot to restore from for one entry."""

    BINDINGS = [
        Binding("enter", "open_focused", "Open snapshot", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
    ]

    def __init__(self, context: AppContext, entry_id: str):
        super().__init__()
        self.context = context
        self.entry_id = entry_id
        self._snapshots: list[SnapshotRow] = []
        self._store_unavailable: str | None = None
        self._resolved: ResolvedEntry | None = None
        self._load()

    # ---------- compose / mount ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static(f"Restore · {self._entry_display_name()}", classes="section-title")

            if self._store_unavailable:
                yield Static(
                    self._store_unavailable,
                    classes="alert",
                    id="restore-empty-state",
                )
            elif not self._snapshots:
                yield Static(
                    "No snapshots for this entry. Back up first (press 'b').",
                    classes="muted",
                    id="restore-empty-state",
                )
            else:
                yield Static("Snapshots (oldest → newest):", classes="muted")
                yield ListView(
                    *(_SnapshotRowItem(row) for row in self._snapshots),
                    id="restore-snapshots-list",
                )

            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LazyServer · Restore"
        self.sub_title = f"snapshots · {self.entry_id}"
        try:
            view = self.query_one("#restore-snapshots-list", ListView)
        except Exception:
            return
        if view.children:
            view.focus()
            # Default focus on the newest snapshot — that's the most
            # common restore target ("undo my last edit").
            view.index = len(view.children) - 1

    # ---------- actions ----------

    def action_open_focused(self) -> None:
        focused = self.focused
        if not isinstance(focused, ListView):
            return
        highlighted = focused.highlighted_child
        if isinstance(highlighted, _SnapshotRowItem) and self._resolved is not None:
            self.app.push_screen(
                RestoreFilesScreen(
                    self.context,
                    self._resolved,
                    snapshot_ts=highlighted.row.timestamp,
                )
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, _SnapshotRowItem) and self._resolved is not None:
            self.app.push_screen(
                RestoreFilesScreen(
                    self.context,
                    self._resolved,
                    snapshot_ts=event.item.row.timestamp,
                )
            )

    # ---------- internal ----------

    def _entry_display_name(self) -> str:
        for entry in self.context.entries:
            if entry.id == self.entry_id:
                return entry.name
        return self.entry_id

    def _load(self) -> None:
        store_path = resolved_backup_store(
            self.context.settings, self.context.target_user
        )
        if store_path is None:
            self._store_unavailable = (
                "No backup store configured.\n"
                "Edit ~/.config/lazyserver/config.toml and set "
                "`backup_store = \"/path/to/store\"`."
            )
            return
        try:
            entry = next(e for e in self.context.entries if e.id == self.entry_id)
        except StopIteration:
            self._store_unavailable = f"Unknown entry id: {self.entry_id}"
            return
        try:
            self._resolved = resolve(
                entry,
                self.context.distro.id,
                target_user=self.context.target_user,
            )
        except ResolutionError as exc:
            self._store_unavailable = f"Cannot resolve {self.entry_id}: {exc}"
            return
        store = make_backup_store(store_path, target_user=self.context.target_user)
        self._snapshots = list_snapshot_rows(store, self.entry_id)


# ---------- files screen (level 2) ----------


class RestoreFilesScreen(Screen):
    """Preview + restore one snapshot's files for one entry.

    The list shows every file the snapshot will overwrite with its
    pre-resolved ownership inline, plus any file_set extras flagged
    ``not touched`` (FR-3.4). Uppercase ``R`` triggers the restore;
    after success a modal surfaces the pre-restore TS + the literal
    undo invocation, then we pop back to the snapshots screen so the
    new pre-restore snapshot is visible in the list.
    """

    BINDINGS = [
        Binding("R", "restore", "Restore (overwrite live files)", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
    ]

    def __init__(
        self,
        context: AppContext,
        resolved: ResolvedEntry,
        *,
        snapshot_ts: str,
    ):
        super().__init__()
        self.context = context
        self.resolved = resolved
        self.snapshot_ts = snapshot_ts
        self._planned: list[PlannedRow] = []
        self._extras: list[ExtraRow] = []
        self._error: str | None = None
        self._build_plan()

    # ---------- compose / mount ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static(
                f"Restore · {self.resolved.entry.name} · {self.snapshot_ts}",
                classes="section-title",
            )

            if self._error:
                yield Static(self._error, classes="alert", id="restore-empty-state")
            elif not self._planned and not self._extras:
                yield Static(
                    "Nothing in this snapshot to restore.",
                    classes="muted",
                    id="restore-empty-state",
                )
            else:
                yield Static(
                    f"Press R to restore — {len(self._planned)} file(s) will be "
                    f"overwritten, {len(self._extras)} extra(s) reported.",
                    classes="muted",
                )
                rows: list[ListItem] = [
                    _PlannedRowItem(r) for r in self._planned
                ]
                rows.extend(_ExtraRowItem(r) for r in self._extras)
                yield ListView(*rows, id="restore-files-list")

            yield Static("", id="restore-result", classes="muted")
            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LazyServer · Restore"
        self.sub_title = f"{self.resolved.entry.id} · {self.snapshot_ts}"
        try:
            view = self.query_one("#restore-files-list", ListView)
        except Exception:
            return
        if view.children:
            view.focus()

    # ---------- actions ----------

    def action_restore(self) -> None:
        """Run the restore. No confirmation by design (NFR-2 + §9)."""
        if not self._planned and not self._extras:
            self._set_result("Nothing to restore.", alert=False)
            return

        store_path = resolved_backup_store(
            self.context.settings, self.context.target_user
        )
        if store_path is None:
            self._set_result("No backup store configured.", alert=True)
            return

        if self.app.dry_run:
            self._set_result(
                "Dry-run: no live files written. "
                f"Would capture pre-restore at {current_timestamp()}{PRE_RESTORE_SUFFIX}.",
                alert=False,
            )
            return

        store = make_backup_store(store_path, target_user=self.context.target_user)
        baselines = BaselineStore.load(store_path, target_user=self.context.target_user)
        resolved_map = {self.resolved.entry.id: self.resolved}
        plan = plan_restore(
            selection=RestoreSelection(
                entry_ids=(self.resolved.entry.id,),
                file_paths=None,
                snapshot_choice=SnapshotChoice(
                    timestamps={self.resolved.entry.id: self.snapshot_ts}
                ),
            ),
            resolved_entries=resolved_map,
            store=store,
        )
        pre_restore_ts = current_timestamp()
        try:
            reports = execute_restore(
                plan,
                store=store,
                baselines=baselines,
                resolved_entries=resolved_map,
                target_user=self.context.target_user,
                pre_restore_timestamp=pre_restore_ts,
            )
        except Exception as exc:
            log.exception("execute_restore crashed in RestoreFilesScreen")
            self._set_result(f"✗ Restore crashed: {exc}", alert=True)
            return

        restored = sum(1 for r in reports if r.outcome is RestoreOutcome.RESTORED)
        failed = sum(
            1
            for r in reports
            if r.outcome
            in (RestoreOutcome.WRITE_FAILED, RestoreOutcome.PRE_SNAPSHOT_FAILED)
        )
        pre_full = f"{pre_restore_ts}{PRE_RESTORE_SUFFIX}"
        self._set_result(
            f"✓ Restored {restored} file(s){'  ⚠ ' + str(failed) + ' failed' if failed else ''}.  "
            f"Pre-restore: {pre_full}",
            alert=bool(failed),
        )
        self.app.push_screen(
            _RestoreReportModal(
                reports=reports,
                pre_restore_ts_full=pre_full,
                entry_ids=(self.resolved.entry.id,) if restored else (),
            )
        )

    # ---------- internal ----------

    def _build_plan(self) -> None:
        store_path = resolved_backup_store(
            self.context.settings, self.context.target_user
        )
        if store_path is None:
            self._error = (
                "No backup store configured.\n"
                "Edit ~/.config/lazyserver/config.toml and set "
                "`backup_store = \"/path/to/store\"`."
            )
            return
        store = make_backup_store(store_path, target_user=self.context.target_user)
        resolved_map = {self.resolved.entry.id: self.resolved}
        plan = plan_restore(
            selection=RestoreSelection(
                entry_ids=(self.resolved.entry.id,),
                file_paths=None,
                snapshot_choice=SnapshotChoice(
                    timestamps={self.resolved.entry.id: self.snapshot_ts}
                ),
            ),
            resolved_entries=resolved_map,
            store=store,
        )
        self._planned, self._extras = build_planned_rows(
            plan,
            resolved_entries=resolved_map,
            target_user=self.context.target_user,
        )

    def _set_result(self, text: str, *, alert: bool) -> None:
        try:
            widget = self.query_one("#restore-result", Static)
        except Exception:
            return
        widget.update(text)
        widget.set_class(alert, "alert")


# ---------- result modal ----------


class _RestoreReportModal(ModalScreen[None]):
    """Post-action report with the copy-paste undo banner.

    Per-file outcomes on top; the pre-restore TS + literal
    ``lsrv restore --entry ID --snapshot <ts>-pre-restore`` line at
    the bottom so the safety net is the last thing the student sees
    before dismissing.
    """

    BINDINGS = [Binding("escape,enter,q", "close", "Close", show=True)]

    def __init__(
        self,
        *,
        reports: list[RestoreReport],
        pre_restore_ts_full: str,
        entry_ids: tuple[str, ...],
    ):
        super().__init__()
        self._reports = reports
        self._pre_full = pre_restore_ts_full
        self._entry_ids = entry_ids

    def compose(self) -> ComposeResult:
        restored = sum(1 for r in self._reports if r.outcome is RestoreOutcome.RESTORED)
        failed = sum(
            1
            for r in self._reports
            if r.outcome
            in (RestoreOutcome.WRITE_FAILED, RestoreOutcome.PRE_SNAPSHOT_FAILED)
        )
        extras = sum(
            1 for r in self._reports if r.outcome is RestoreOutcome.EXTRA_REPORTED
        )
        with Vertical(classes="modal-body"):
            yield Static("Restore report", classes="entry-title")
            yield Static(
                f"{restored} restored · {failed} failed · {extras} extra(s) reported",
                classes="muted",
            )
            with VerticalScroll(classes="command-output-scroll"):
                for rep in self._reports:
                    text, classes = _format_report_row(rep)
                    yield Static(text, classes=classes)

            if self._entry_ids:
                yield Static("", classes="muted")
                for line in format_undo_banner(self._pre_full, self._entry_ids):
                    # The banner gets the accent style so the copy-paste
                    # invocation is impossible to miss.
                    yield Static(line, classes="action-hint")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)


def _format_report_row(rep: RestoreReport) -> tuple[str, ...]:
    """Return (text, classes) tuple for one report row in the modal."""
    if rep.outcome is RestoreOutcome.RESTORED:
        assert rep.item is not None
        return (
            f"  ✓ {rep.item.source_path}    "
            f"uid={rep.chosen_uid} gid={rep.chosen_gid} mode={oct(rep.chosen_mode or 0)}",
            "command-output-text",
        )
    if rep.outcome is RestoreOutcome.PRE_SNAPSHOT_FAILED:
        assert rep.item is not None
        return (
            f"  ✗ {rep.item.source_path} — pre-restore snapshot failed: "
            f"{rep.error or 'unknown'} (live file untouched)",
            "command-output-text alert",
        )
    if rep.outcome is RestoreOutcome.WRITE_FAILED:
        assert rep.item is not None
        pre = ""
        if rep.pre_snapshot_ref is not None:
            pre = f"  (pre-restore snapshot at {rep.pre_snapshot_ref.timestamp} is intact)"
        return (
            f"  ✗ {rep.item.source_path} — write failed: {rep.error or 'unknown'}{pre}",
            "command-output-text alert",
        )
    if rep.outcome is RestoreOutcome.EXTRA_REPORTED:
        assert rep.extra is not None
        return (
            f"  · {rep.extra.path}    extra (not touched, FR-3.4)",
            "command-output-text",
        )
    return (f"  ? {rep.outcome.value}", "command-output-text")
