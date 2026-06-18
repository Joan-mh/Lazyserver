"""File / file-set detail — description, example, edit (FR-1.3, FR-4.3).

Pressing Enter on a fixed file resolves the user's editor (FR-7.3),
suspends the TUI, launches the editor against the resolved path, and on
return recomputes the file's SHA-256 (FR-2.1) to report whether the file
changed. The full pending-set storage lands in Phase 4; for now the
screen surfaces the detection so the workflow is exercised end-to-end.

File-set rendering lists the matching files on disk and lets the user
edit any of them. Creating a brand-new file in a set (FR-1.7/8/9) is
Phase 3e.
"""

from __future__ import annotations

import glob
from pathlib import Path

from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..backup.checksums import sha256_of
from ..backup.create import plan_ownership
from ..editor import launch_editor
from ..tconf.model import Entry
from ..tconf.resolve import ResolvedFile, ResolvedFileSet


class _SetMemberRow(ListItem):
    def __init__(self, path: Path):
        super().__init__(Label(str(path)))
        self.path = path


class FileScreen(Screen):
    """One managed file (fixed or set) — purpose, location, example, edit."""

    BINDINGS = [
        Binding("enter", "edit_focused", "Edit", show=True),
        Binding("n", "new_in_set", "New file", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
    ]

    def __init__(
        self,
        context: AppContext,
        entry: Entry,
        payload: ResolvedFile | ResolvedFileSet,
    ):
        super().__init__()
        self.context = context
        self.entry = entry
        self.payload = payload

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll():
            if isinstance(self.payload, ResolvedFile):
                yield from self._compose_file(self.payload)
            else:
                yield from self._compose_file_set(self.payload)
            yield Static("", id="edit-result", classes="muted")
            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def _compose_file(self, f: ResolvedFile):
        yield Static(f.id, classes="file-title")
        yield Static(f.path, classes="file-path")
        if f.optional:
            yield Static("(optional — may legitimately be absent)", classes="muted")
        yield Static(f.description.strip(), classes="file-description")
        if f.example:
            yield Static("Example", classes="section-title")
            yield Static(f.example.rstrip(), classes="example")

    def _compose_file_set(self, fs: ResolvedFileSet):
        yield Static(f"{fs.id}  (file set)", classes="file-title")
        yield Static(f"{fs.directory}/{fs.pattern}", classes="file-path")
        yield Static(fs.description.strip(), classes="file-description")
        if fs.example:
            yield Static("Example file", classes="section-title")
            yield Static(fs.example.rstrip(), classes="example")

        members = _expand_set(fs)
        yield Static("Existing files in this set", classes="section-title")
        if members:
            yield ListView(
                *(_SetMemberRow(p) for p in members), id="set-members"
            )
        else:
            yield Static("(none on disk)", classes="muted")

    def on_mount(self) -> None:
        self.title = f"LazyServer · {self.entry.name}"
        self.sub_title = self.payload.id
        # If we're showing a set with members, focus the list so Enter edits.
        try:
            view = self.query_one("#set-members", ListView)
        except Exception:
            return
        if view.children:
            view.focus()

    def action_edit_focused(self) -> None:
        """Edit the file in focus.

        For ResolvedFile: edit the resolved path directly.
        For ResolvedFileSet: edit the currently-highlighted set member.
        """
        if isinstance(self.payload, ResolvedFile):
            self._edit_path(Path(self.payload.path))
            return
        try:
            view = self.query_one("#set-members", ListView)
        except Exception:
            return
        highlighted = view.highlighted_child
        if isinstance(highlighted, _SetMemberRow):
            self._edit_path(highlighted.path)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, _SetMemberRow):
            self._edit_path(event.item.path)

    def action_new_in_set(self) -> None:
        """Open the create modal for a file_set entry (FR-1.7/1.8/1.9)."""
        if not isinstance(self.payload, ResolvedFileSet):
            return
        from .create_screen import NewFileScreen

        fs = self.payload
        plan = plan_ownership(
            entry_kind=self.entry.kind,
            directory=Path(fs.directory),
            target_user=self.context.target_user,
            explicit_owner=fs.owner,
            explicit_group=fs.group,
            explicit_mode=fs.mode,
        )
        screen = NewFileScreen(self.entry, fs, plan, dry_run=self.app.dry_run)
        self.app.push_screen(screen, self._after_create)

    def _after_create(self, created: Path | None) -> None:
        if created is None:
            return
        self._edit_path(created)

    def _edit_path(self, path: Path) -> None:
        before = sha256_of(path)
        try:
            with self.app.suspend():
                result = launch_editor(
                    self.context.settings, path, dry_run=self.app.dry_run
                )
        except SuspendNotSupported:
            # Headless test environment; the editor still launches but the
            # TUI is not suspended.
            result = launch_editor(
                self.context.settings, path, dry_run=self.app.dry_run
            )
        after = sha256_of(path)
        widget = self.query_one("#edit-result", Static)
        widget.update(_describe_edit(path, before, after, result.dry_run))


def _describe_edit(
    path: Path, before: str | None, after: str | None, dry_run: bool
) -> str:
    if dry_run:
        return f"(dry-run) editor not launched for {path}"
    if before is None and after is None:
        return f"file still absent: {path}"
    if before is None and after is not None:
        return f"✓ created {path} (sha256 {after[:12]})"
    if before is not None and after is None:
        return f"⚠ {path} was deleted in the editor"
    if before != after:
        return f"✓ modified {path} (sha256 {before[:12]} → {after[:12]})"
    return f"no change: {path}"


def _expand_set(fs: ResolvedFileSet) -> list[Path]:
    """Expand the file_set glob within its resolved directory.

    `**` is honored only if the user wrote it in the pattern (schema §3b).
    Returns sorted absolute paths, files only.
    """
    base = Path(fs.directory)
    if not base.is_dir():
        return []
    recursive = "**" in fs.pattern
    matches = glob.glob(str(base / fs.pattern), recursive=recursive)
    return sorted(Path(m) for m in matches if Path(m).is_file())
