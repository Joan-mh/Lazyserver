"""Backup screen — grouped pending list + scoped backup (FR-2.3/2.4).

Layout: one screen, one ListView, rows grouped by entry. Selection
state lives on the screen; the engine in ``backup.run`` does the
actual work. The screen's job is to let the student pick a scope and
report what happened.

Grouping rule (decided in design pass): show an entry iff it has at
least one NEW/CHANGED/MISSING item. Entries that are entirely clean
(nothing pending) and entries whose only pending items are
ABSENT_REQUIRED (proxy for "service not installed") are both hidden,
so a typical machine — where most catalogued services aren't installed
— doesn't drown the screen in noise. The HomeScreen still shows the
full catalogue.

Scope (FR-2.3): three scopes fall out of one selection mechanism.
Space toggles a file row (scope 3: by files). Space on an entry-header
row toggles every backup-eligible file under it (scope 2: by entries).
``B`` ignores selection and runs against every eligible item across
every visible entry (scope 1: all pending).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..config import resolved_backup_store
from ..backup._fsutil import ensure_owned_dir
from ..backup.pending import (
    BaselineStore,
    PendingItem,
    PendingStatus,
    scan_all,
)
from ..backup.run import (
    BackupOutcome,
    BackupReport,
    backup_files,
    current_timestamp,
)
from ..backup.store import make_backup_store
from ..tconf.resolve import ResolutionError, ResolvedEntry, resolve

log = logging.getLogger("lazyserver.ui.backup_screen")

# Short status codes for the row label. ABSENT_OPTIONAL and UNCHANGED
# never reach the renderer (filtered out before grouping).
_STATUS_CODE: dict[PendingStatus, str] = {
    PendingStatus.NEW: "NEW ",
    PendingStatus.CHANGED: "CHG ",
    PendingStatus.MISSING: "MIS ",
    PendingStatus.ABSENT_REQUIRED: "!REQ",
}


# ---------- pure helpers (unit-testable without Textual) ----------


@dataclass
class EntryGroup:
    """One entry's worth of rows for the backup screen."""

    entry_id: str
    entry_name: str
    items: list[PendingItem]

    def eligible(self) -> list[PendingItem]:
        return [i for i in self.items if i.status.is_backup_eligible()]


def is_entry_actionable(items: Iterable[PendingItem]) -> bool:
    """True iff this entry should appear on the backup screen.

    Combines two hide rules into one predicate: clean entries (no
    pending items) and uninstalled-proxy entries (only ABSENT_REQUIRED)
    are both hidden. An entry with at least one NEW/CHANGED/MISSING
    item is shown — mixed entries still surface their ABSENT_REQUIRED
    rows so the student sees the whole picture.
    """
    return any(
        i.status in (PendingStatus.NEW, PendingStatus.CHANGED, PendingStatus.MISSING)
        for i in items
    )


def group_for_display(
    items: list[PendingItem], names: dict[str, str]
) -> list[EntryGroup]:
    """Bucket scan output by entry_id, drop hidden entries, sort.

    Item statuses already-not-pending (UNCHANGED, ABSENT_OPTIONAL) are
    dropped at the row level so the visible list is *only* the four
    rendered codes.
    """
    by_entry: dict[str, list[PendingItem]] = {}
    for item in items:
        if not item.status.is_pending():
            continue
        by_entry.setdefault(item.entry_id, []).append(item)

    groups: list[EntryGroup] = []
    for entry_id in sorted(by_entry):
        rows = sorted(by_entry[entry_id], key=lambda i: str(i.path))
        if not is_entry_actionable(rows):
            continue
        groups.append(
            EntryGroup(
                entry_id=entry_id,
                entry_name=names.get(entry_id, entry_id),
                items=rows,
            )
        )
    return groups


def summarize_group(group: EntryGroup) -> str:
    """Human-readable group header right side: '3 pending · 2 new, 1 changed'."""
    counts: dict[PendingStatus, int] = {}
    for i in group.items:
        counts[i.status] = counts.get(i.status, 0) + 1
    pieces: list[str] = []
    for status, label in (
        (PendingStatus.NEW, "new"),
        (PendingStatus.CHANGED, "changed"),
        (PendingStatus.MISSING, "missing"),
        (PendingStatus.ABSENT_REQUIRED, "absent"),
    ):
        if counts.get(status):
            pieces.append(f"{counts[status]} {label}")
    total = sum(counts.values())
    return f"{total} pending · {', '.join(pieces)}"


def latest_snapshot(baselines: BaselineStore, entry_ids: Iterable[str]) -> str | None:
    """Most-recent snapshot timestamp across the ledger, or None if empty."""
    seen: list[str] = []
    for entry_id in entry_ids:
        for _path, baseline in baselines.iter_entry(entry_id):
            seen.append(baseline.snapshot)
    return max(seen) if seen else None


# ---------- selection model ----------


@dataclass
class Selection:
    """Tracks selected (entry_id, path) pairs for backup.

    Toggling an entry-header row fans out to its backup-eligible
    children (FR-2.3 scope 2). Toggling a file row toggles that file
    (scope 3). Non-eligible rows (MISSING, ABSENT_REQUIRED) are always
    silently skipped — selecting them would be a no-op anyway since
    the engine wouldn't act on them.
    """

    _keys: set[tuple[str, Path]] = field(default_factory=set)

    def toggle_file(self, entry_id: str, item: PendingItem) -> None:
        if not item.status.is_backup_eligible():
            return
        key = (entry_id, item.path)
        if key in self._keys:
            self._keys.remove(key)
        else:
            self._keys.add(key)

    def toggle_entry(self, group: EntryGroup) -> None:
        keys = {(group.entry_id, i.path) for i in group.eligible()}
        if not keys:
            return
        if keys.issubset(self._keys):
            self._keys -= keys
        else:
            self._keys |= keys

    def select_all(self, groups: Iterable[EntryGroup]) -> None:
        for g in groups:
            for i in g.eligible():
                self._keys.add((g.entry_id, i.path))

    def clear(self) -> None:
        self._keys.clear()

    def is_selected(self, entry_id: str, path: Path) -> bool:
        return (entry_id, path) in self._keys

    def selected_items(self, groups: Iterable[EntryGroup]) -> list[PendingItem]:
        """Pending items currently selected, in group/file order."""
        out: list[PendingItem] = []
        for g in groups:
            for i in g.items:
                if (g.entry_id, i.path) in self._keys:
                    out.append(i)
        return out

    def count(self) -> int:
        return len(self._keys)


# ---------- row widgets ----------


class _EntryHeaderRow(ListItem):
    def __init__(self, group: EntryGroup):
        self._label = Static("", classes="backup-entry-header")
        super().__init__(self._label)
        self.group = group

    def update_render(self, selection: Selection) -> None:
        self._label.update(
            f"{self.group.entry_name}  ({summarize_group(self.group)})"
        )


class _FileRow(ListItem):
    def __init__(self, entry_id: str, item: PendingItem):
        self._label = Static("", classes="backup-file-row")
        super().__init__(self._label)
        self.entry_id = entry_id
        self.item = item

    def update_render(self, selection: Selection) -> None:
        marker = self._marker(selection)
        code = _STATUS_CODE.get(self.item.status, "????")
        self._label.update(f"  {marker} {code}  {self.item.path}")

    def _marker(self, selection: Selection) -> str:
        if not self.item.status.is_backup_eligible():
            return "[-]"
        return "[x]" if selection.is_selected(self.entry_id, self.item.path) else "[ ]"


# ---------- modals ----------


class _PlanModal(ModalScreen[None]):
    """Dry-run plan: what would be backed up if this were a real run."""

    BINDINGS = [Binding("escape,enter,q", "close", "Close", show=True)]

    def __init__(self, items: list[PendingItem]):
        super().__init__()
        self._items = items

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-body"):
            yield Static("Dry-run plan", classes="entry-title")
            yield Static(
                f"would back up {len(self._items)} file(s):", classes="muted"
            )
            with VerticalScroll(classes="command-output-scroll"):
                for i in self._items:
                    code = _STATUS_CODE.get(i.status, "????")
                    yield Static(f"  {code}  {i.path}", classes="command-output-text")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)


class _ReportModal(ModalScreen[None]):
    """Failure-only post-run report: per-file outcomes with error text."""

    BINDINGS = [Binding("escape,enter,q", "close", "Close", show=True)]

    def __init__(self, reports: list[BackupReport], timestamp: str):
        super().__init__()
        self._reports = reports
        self._timestamp = timestamp

    def compose(self) -> ComposeResult:
        backed = sum(1 for r in self._reports if r.outcome is BackupOutcome.BACKED_UP)
        failed = sum(1 for r in self._reports if r.outcome is BackupOutcome.FAILED)
        with Vertical(classes="modal-body"):
            yield Static(f"Backup report · {self._timestamp}", classes="entry-title")
            yield Static(
                f"{backed} backed up, {failed} failed", classes="muted"
            )
            with VerticalScroll(classes="command-output-scroll"):
                for rep in self._reports:
                    if rep.outcome is BackupOutcome.BACKED_UP:
                        yield Static(
                            f"  ✓ {rep.item.path}", classes="command-output-text"
                        )
                    elif rep.outcome is BackupOutcome.FAILED:
                        yield Static(
                            f"  ✗ {rep.item.path} — {rep.error or 'unknown error'}",
                            classes="command-output-text alert",
                        )
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)


# ---------- screen ----------


class BackupScreen(Screen):
    """Pending list + scoped backup, per FR-2.3/2.4."""

    BINDINGS = [
        Binding("space", "toggle", "Toggle", show=True),
        Binding("a", "select_all", "All", show=True),
        Binding("n", "clear", "Clear", show=True),
        Binding("b", "backup_selected", "Backup selected", show=True),
        Binding("B", "backup_all", "Backup all", show=True),
        Binding("r", "rescan", "Rescan", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
    ]

    def __init__(self, context: AppContext):
        super().__init__()
        self.context = context
        self._selection = Selection()
        self._resolved: list[ResolvedEntry] = self._resolve_entries()
        self._groups: list[EntryGroup] = []
        self._latest_snapshot: str | None = None
        self._scan()

    # ---------- compose / mount ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static("Backup", classes="section-title")

            store_path = self._store_path()
            if store_path is None:
                yield Static(
                    "No backup store configured.\n"
                    "Edit ~/.config/lazyserver/config.toml and set "
                    "`backup_store = \"/path/to/store\"`.",
                    classes="alert",
                    id="backup-empty-state",
                )
            elif not self._groups:
                hint = (
                    f"Latest snapshot: {self._latest_snapshot}"
                    if self._latest_snapshot
                    else "no snapshots yet"
                )
                yield Static(
                    f"Everything up to date.  ({hint})",
                    classes="muted",
                    id="backup-empty-state",
                )
            else:
                yield ListView(
                    *self._build_rows(),
                    id="backup-list",
                )

            yield Static("", id="backup-result", classes="muted")
            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LazyServer · Backup"
        self.sub_title = self._store_subtitle()
        try:
            view = self.query_one("#backup-list", ListView)
        except Exception:
            return
        if view.children:
            view.focus()

    # ---------- actions ----------

    def action_toggle(self) -> None:
        focused = self.focused
        if not isinstance(focused, ListView):
            return
        highlighted = focused.highlighted_child
        if isinstance(highlighted, _FileRow):
            self._selection.toggle_file(highlighted.entry_id, highlighted.item)
        elif isinstance(highlighted, _EntryHeaderRow):
            self._selection.toggle_entry(highlighted.group)
        self._render_rows()

    def action_select_all(self) -> None:
        self._selection.select_all(self._groups)
        self._render_rows()

    def action_clear(self) -> None:
        self._selection.clear()
        self._render_rows()

    def action_backup_selected(self) -> None:
        items = self._selection.selected_items(self._groups)
        if not items:
            self._set_result("Nothing selected. Use Space, or press B for all.", alert=False)
            return
        self._run_backup(items)

    def action_backup_all(self) -> None:
        items = [i for g in self._groups for i in g.eligible()]
        if not items:
            self._set_result("Nothing eligible to back up.", alert=False)
            return
        self._run_backup(items)

    def action_rescan(self) -> None:
        self._scan()
        self._rebuild_list()
        self._set_result("Rescanned.", alert=False)

    # ---------- engine wiring ----------

    def _run_backup(self, items: list[PendingItem]) -> None:
        if self.app.dry_run:
            self.app.push_screen(_PlanModal(items))
            return

        store_path = self._store_path()
        if store_path is None:
            self._set_result("No backup store configured.", alert=True)
            return

        created = ensure_owned_dir(store_path, self.context.target_user)
        create_notice = (
            f"Created backup store at {store_path}\n" if created else ""
        )
        baselines = BaselineStore.load(store_path, target_user=self.context.target_user)
        store = make_backup_store(store_path, target_user=self.context.target_user)
        timestamp = current_timestamp()
        try:
            reports = backup_files(
                items=items, store=store, baselines=baselines, timestamp=timestamp,
            )
        except Exception as exc:
            log.exception("backup_files crashed")
            self._set_result(
                f"{create_notice}✗ Backup crashed: {exc}", alert=True
            )
            return

        backed = sum(1 for r in reports if r.outcome is BackupOutcome.BACKED_UP)
        failed = sum(1 for r in reports if r.outcome is BackupOutcome.FAILED)
        if failed:
            self._set_result(
                f"{create_notice}⚠ Backed up {backed} file(s), {failed} failed at {timestamp}",
                alert=True,
            )
            self.app.push_screen(_ReportModal(reports, timestamp))
        else:
            self._set_result(
                f"{create_notice}✓ Backed up {backed} file(s) at {timestamp}",
                alert=bool(create_notice),
            )

        # Re-scan so just-backed-up rows disappear and selection is reset.
        self._selection.clear()
        self._scan()
        self._rebuild_list()

    # ---------- internal ----------

    def _resolve_entries(self) -> list[ResolvedEntry]:
        resolved: list[ResolvedEntry] = []
        for entry in self.context.entries:
            try:
                resolved.append(
                    resolve(
                        entry,
                        self.context.distro.id,
                        target_user=self.context.target_user,
                    )
                )
            except ResolutionError as exc:
                log.warning("backup screen: skip %s — %s", entry.id, exc)
        return resolved

    def _scan(self) -> None:
        store_path = self._store_path()
        if store_path is None:
            self._groups = []
            self._latest_snapshot = None
            return
        baselines = BaselineStore.load(store_path)
        items = scan_all(self._resolved, baselines)
        names = {r.entry.id: r.entry.name for r in self._resolved}
        self._groups = group_for_display(items, names)
        self._latest_snapshot = latest_snapshot(
            baselines, (r.entry.id for r in self._resolved)
        )

    def _build_rows(self) -> list[ListItem]:
        rows: list[ListItem] = []
        for group in self._groups:
            header = _EntryHeaderRow(group)
            header.update_render(self._selection)
            rows.append(header)
            for item in group.items:
                row = _FileRow(group.entry_id, item)
                row.update_render(self._selection)
                rows.append(row)
        return rows

    def _render_rows(self) -> None:
        """Refresh markers in-place without rebuilding the list."""
        try:
            view = self.query_one("#backup-list", ListView)
        except Exception:
            return
        for child in view.children:
            if isinstance(child, (_FileRow, _EntryHeaderRow)):
                child.update_render(self._selection)

    def _rebuild_list(self) -> None:
        """Full teardown + repopulate. Used after rescan/backup so removed
        rows disappear and the empty-state branch decision stays sticky
        (we don't switch widget identity mid-mount)."""
        try:
            view = self.query_one("#backup-list", ListView)
        except Exception:
            return
        view.clear()
        for row in self._build_rows():
            view.append(row)

    def _set_result(self, text: str, *, alert: bool) -> None:
        try:
            widget = self.query_one("#backup-result", Static)
        except Exception:
            return
        widget.update(text)
        widget.set_class(alert, "alert")

    def _store_path(self) -> Path | None:
        return resolved_backup_store(
            self.context.settings, self.context.target_user
        )

    def _store_subtitle(self) -> str:
        sp = self._store_path()
        return f"store: {sp}" if sp else "store: not configured"
