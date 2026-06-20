"""Recovery TUI — full system rebuild (FR-5.3, Phase 6).

Mirrors the CLI: builds the same ``RecoveryPlan``, runs the same
``execute_recovery`` orchestrator, writes the same on-disk artifacts
via the shared ``write_recovery_artifacts`` helper. The screen is a
thin shell over the engine — engineering parity with ``lsrv recover
--all`` is the point (a TUI run and a CLI run on the same state must
produce byte-identical log + JSON).

Two keys, mirroring backup/restore convention:

  * ``r`` — recover the **focused** entry only (single-entry plan).
  * ``R`` — recover **every** entry (the disaster-recovery flow).

After completion, the result modal surfaces the per-entry rollup and
the two artifact paths so the operator can ``cat`` them — the
recovery analogue of restore's copy-paste undo banner.

Live-updating status column during a run is *not* implemented: the
synchronous engine runs in one shot and the modal lands when done.
The status column on the list view *is* updated from the report once
the run finishes, so a second run on the same screen sees the prior
outcome at a glance.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..backup.pending import BaselineStore
from ..backup.run import current_timestamp
from ..backup.store import make_backup_store
from ..config import resolved_backup_store
from ..recovery.plan import build_recovery_plan
from ..recovery.report import (
    ENTRY_FAILED,
    ENTRY_OK,
    ENTRY_PARTIAL,
    ENTRY_SKIPPED,
    ENTRY_WOULD_RUN,
    EntryResult,
    RecoveryReport,
    summarise,
    write_recovery_artifacts,
)
from ..recovery.run import execute_recovery
from ..tconf.resolve import ResolutionError, ResolvedEntry, resolve

log = logging.getLogger("lazyserver.ui.recover_screen")


_STATUS_GLYPHS = {
    ENTRY_OK: "✓ OK",
    ENTRY_PARTIAL: "◐ PARTIAL",
    ENTRY_FAILED: "✗ FAILED",
    ENTRY_SKIPPED: "· SKIPPED",
    ENTRY_WOULD_RUN: "→ WOULD-RUN",
}


def format_entry_row(entry_id: str, kind: str, status: str | None) -> str:
    """One row in the entry list — id · kind · status.

    ``status`` is None before any run; populated after the first run
    so re-pressing r/R updates the column to the latest outcome.
    """
    tag = _STATUS_GLYPHS.get(status or "", "") if status else ""
    pad = "  " if not tag else "  "
    return f"  {entry_id:<20} {kind:<8}{pad}{tag}"


class _EntryRow(ListItem):
    """Selectable entry row; the label is re-rendered after each run."""

    def __init__(self, entry_id: str, kind: str):
        self.entry_id = entry_id
        self.kind = kind
        self._label = Static(format_entry_row(entry_id, kind, None))
        super().__init__(self._label)

    def update_status(self, status: str | None) -> None:
        self._label.update(format_entry_row(self.entry_id, self.kind, status))


class RecoverScreen(Screen):
    """Per-entry status list plus r/R to recover focused/all."""

    BINDINGS = [
        Binding("r", "recover_focused", "Recover focused", show=True),
        Binding("R", "recover_all", "Recover all", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
    ]

    def __init__(self, context: AppContext):
        super().__init__()
        self.context = context
        # Resolve every entry up front so the list reflects what the
        # planner would see. Unresolvable entries are dropped (same
        # posture as the CLI); the list shows only what could run.
        self._resolved_map: dict[str, ResolvedEntry] = {}
        for entry in context.entries:
            try:
                self._resolved_map[entry.id] = resolve(
                    entry, context.distro.id, target_user=context.target_user
                )
            except ResolutionError as exc:
                log.warning("recover screen: skip %s: %s", entry.id, exc)
        # Alphabetical by id — same order as build_recovery_plan.
        self._ordered_ids = sorted(self._resolved_map)
        self._store_unavailable: str | None = None
        self._verify_store()

    # ---------- compose / mount ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static("Recovery", classes="section-title")
            yield Static(
                "r = recover focused entry · R = recover all "
                f"({len(self._ordered_ids)} entries)",
                classes="muted",
            )

            if self._store_unavailable:
                yield Static(
                    self._store_unavailable,
                    classes="alert",
                    id="recover-empty-state",
                )
            elif not self._ordered_ids:
                yield Static(
                    f"No entries resolved for distro "
                    f"{self.context.distro.id!r} — nothing to recover.",
                    classes="muted",
                    id="recover-empty-state",
                )
            else:
                yield ListView(
                    *(
                        _EntryRow(eid, self._resolved_map[eid].entry.kind)
                        for eid in self._ordered_ids
                    ),
                    id="recover-list",
                )

            yield Static("", id="recover-result", classes="muted")
            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LazyServer · Recover"
        self.sub_title = f"distro: {self.context.distro.id}"
        try:
            view = self.query_one("#recover-list", ListView)
        except Exception:
            return
        if view.children:
            view.focus()

    # ---------- actions ----------

    def action_recover_focused(self) -> None:
        focused = self.focused
        if not isinstance(focused, ListView):
            self._set_result("Focus the entries list, then press r.", alert=False)
            return
        highlighted = focused.highlighted_child
        if not isinstance(highlighted, _EntryRow):
            self._set_result("Nothing focused.", alert=False)
            return
        self._run_recovery(entry_ids={highlighted.entry_id})

    def action_recover_all(self) -> None:
        self._run_recovery(entry_ids=None)

    # ---------- engine ----------

    def _run_recovery(self, *, entry_ids: set[str] | None) -> None:
        if self._store_unavailable:
            self._set_result(self._store_unavailable, alert=True)
            return
        if not self._resolved_map:
            self._set_result("No entries to recover.", alert=False)
            return

        store_path = resolved_backup_store(
            self.context.settings, self.context.target_user
        )
        # Already validated in _verify_store, but assert for type-checkers.
        assert store_path is not None

        if entry_ids is None:
            resolved_subset = self._resolved_map
        else:
            resolved_subset = {
                eid: self._resolved_map[eid]
                for eid in entry_ids
                if eid in self._resolved_map
            }

        store = make_backup_store(store_path, target_user=self.context.target_user)
        baselines = BaselineStore.load(
            store_path, target_user=self.context.target_user
        )
        plan = build_recovery_plan(
            resolved_entries=resolved_subset.values(),
            store=store,
            distro_id=self.context.distro.id,
        )
        timestamp = current_timestamp()
        try:
            report = execute_recovery(
                plan,
                resolved_entries=resolved_subset,
                store=store,
                baselines=baselines,
                target_user=self.context.target_user,
                timestamp=timestamp,
                dry_run=self.app.dry_run,
            )
        except Exception as exc:
            log.exception("execute_recovery crashed in RecoverScreen")
            self._set_result(f"✗ Recovery crashed: {exc}", alert=True)
            return

        # Write artifacts via the same helper the CLI uses — byte-
        # identical files for the same in-memory report.
        try:
            log_path, json_path = write_recovery_artifacts(
                report,
                store_root=store_path,
                target_user=self.context.target_user,
            )
        except Exception as exc:
            log.exception("write_recovery_artifacts crashed")
            log_path = json_path = None  # type: ignore[assignment]
            self._set_result(
                f"✓ Ran, but artifact write failed: {exc}", alert=True
            )

        # Update the status column on the list view so a follow-up
        # run sees the prior outcome at a glance.
        self._update_row_statuses(report)

        # Result summary on the screen itself; full report in the modal.
        summary = summarise(report.entries)
        self._set_result(
            f"{summary[ENTRY_OK]} OK · {summary[ENTRY_PARTIAL]} partial · "
            f"{summary[ENTRY_FAILED]} failed · {summary[ENTRY_SKIPPED]} skipped",
            alert=summary[ENTRY_FAILED] + summary[ENTRY_PARTIAL] > 0,
        )
        self.app.push_screen(
            _RecoveryReportModal(
                report=report,
                log_path=log_path,
                json_path=json_path,
            )
        )

    # ---------- internal ----------

    def _verify_store(self) -> None:
        store_path = resolved_backup_store(
            self.context.settings, self.context.target_user
        )
        if store_path is None:
            self._store_unavailable = (
                "No backup store configured.\n"
                "Edit ~/.config/lazyserver/config.toml and set "
                "`backup_store = \"/path/to/store\"`."
            )

    def _update_row_statuses(self, report: RecoveryReport) -> None:
        try:
            view = self.query_one("#recover-list", ListView)
        except Exception:
            return
        status_by_id = {e.entry_id: e.status for e in report.entries}
        for child in view.children:
            if isinstance(child, _EntryRow):
                status = status_by_id.get(child.entry_id)
                if status is not None:
                    child.update_status(status)

    def _set_result(self, text: str, *, alert: bool) -> None:
        try:
            widget = self.query_one("#recover-result", Static)
        except Exception:
            return
        widget.update(text)
        widget.set_class(alert, "alert")


# ---------- result modal ----------


class _RecoveryReportModal(ModalScreen[None]):
    """Post-run report with the two artifact paths surfaced loud.

    Mirrors restore's modal: per-entry rows on top, summary line,
    then the artifact paths at the bottom — the recovery analogue of
    restore's copy-paste undo banner (the path the operator will
    ``cat`` to read the full log / parse the JSON).
    """

    BINDINGS = [Binding("escape,enter,q", "close", "Close", show=True)]

    def __init__(
        self,
        *,
        report: RecoveryReport,
        log_path,
        json_path,
    ):
        super().__init__()
        self._report = report
        self._log_path = log_path
        self._json_path = json_path

    def compose(self) -> ComposeResult:
        summary = summarise(self._report.entries)
        with Vertical(classes="modal-body"):
            title = "Recovery report"
            if self._report.dry_run:
                title += "  [DRY RUN]"
            yield Static(title, classes="entry-title")
            yield Static(
                f"{summary[ENTRY_OK]} OK · {summary[ENTRY_PARTIAL]} partial · "
                f"{summary[ENTRY_FAILED]} failed · {summary[ENTRY_SKIPPED]} skipped",
                classes="muted",
            )
            with VerticalScroll(classes="command-output-scroll"):
                for entry in self._report.entries:
                    yield Static(
                        _format_entry_summary_row(entry),
                        classes=_row_classes(entry),
                    )

            if self._log_path is not None:
                yield Static("", classes="muted")
                yield Static("Artifacts written:", classes="muted")
                yield Static(f"  log:  {self._log_path}", classes="action-hint")
                yield Static(f"  json: {self._json_path}", classes="action-hint")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)


def _format_entry_summary_row(entry: EntryResult) -> str:
    glyph = _STATUS_GLYPHS.get(entry.status, entry.status)
    step_tags = [f"{s.name}={s.status}" for s in entry.steps]
    return f"  {glyph:<14} {entry.entry_id:<20} {' · '.join(step_tags)}"


def _row_classes(entry: EntryResult) -> str:
    if entry.status in (ENTRY_FAILED, ENTRY_PARTIAL):
        return "command-output-text alert"
    return "command-output-text"
