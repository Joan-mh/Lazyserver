"""Entry detail — description, managed files, and service actions
(FR-1.2, FR-1.5, FR-4.3).

Action keybindings fire immediately, no confirmation prompt (NFR-2 +
spec §9 Deployment assumptions). The runner respects `self.app.dry_run`
so a student can experiment safely by toggling 'd'.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..services.control import UnsupportedActionError, execute_action
from ..tconf.model import Entry
from ..tconf.resolve import (
    FileAlias,
    ResolutionError,
    ResolvedEntry,
    ResolvedFile,
    ResolvedFileSet,
    resolve,
)
from .command_output import (
    CommandOutputModal,
    format_inline_action_summary,
    should_show_output_modal,
)

# Numeric quick-keys for the standard service actions. Order matches the
# spec FR-1.5 listing so the footer reads start → stop → restart → reload
# → enable → disable → status from left to right.
_ACTION_BINDINGS: tuple[tuple[str, str], ...] = (
    ("1", "start"),
    ("2", "stop"),
    ("3", "restart"),
    ("4", "reload"),
    ("5", "enable"),
    ("6", "disable"),
    ("7", "status"),
)


class _FileRow(ListItem):
    def __init__(self, label: str, payload: ResolvedFile | ResolvedFileSet):
        super().__init__(Label(label))
        self.payload = payload


class EntryScreen(Screen):
    """One entry — its description, the resolved managed files, alias notes."""

    BINDINGS = [
        Binding("enter", "open_focused", "Open file", show=True),
        Binding("backspace,escape", "app.pop_screen", "Back", show=True),
        *(
            Binding(key, f"do_action('{action}')", action, show=True)
            for key, action in _ACTION_BINDINGS
        ),
    ]

    def __init__(self, context: AppContext, entry: Entry):
        super().__init__()
        self.context = context
        self.entry = entry
        self.resolved: ResolvedEntry | None = None
        self.resolution_error: str | None = None
        try:
            self.resolved = resolve(
                entry, context.distro.id, target_user=context.target_user
            )
        except ResolutionError as exc:
            self.resolution_error = str(exc)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll():
            yield Static(self.entry.name, classes="entry-title")
            if self.entry.category:
                yield Static(f"category: {self.entry.category}", classes="muted")
            if self.entry.docs_url:
                yield Static(f"docs: {self.entry.docs_url}", classes="muted")
            yield Static(self.entry.description.strip(), classes="entry-description")

            if self.resolution_error:
                yield Static(
                    f"⚠ {self.resolution_error}",
                    classes="alert",
                )
            else:
                assert self.resolved is not None
                yield from self._compose_resolved(self.resolved)

            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def _compose_resolved(self, r: ResolvedEntry) -> ComposeResult:
        if r.service_unit:
            yield Static(
                f"service_unit: {r.service_unit}    package: {r.package}",
                classes="muted",
            )
        else:
            yield Static(f"package: {r.package}", classes="muted")

        for alias in r.aliases:
            yield Static(f"ℹ {alias.note}", classes="alias-note")

        if r.files or r.file_sets:
            yield Static("Managed files", classes="section-title")
            rows: list[_FileRow] = []
            for f in r.files:
                rows.append(_FileRow(self._format_file_row(f, r.aliases), f))
            for fs in r.file_sets:
                rows.append(
                    _FileRow(f"{fs.id}    {fs.directory}/{fs.pattern}    (set)", fs)
                )
            yield ListView(*rows, id="files-list")

        if r.actions:
            yield Static(
                "Actions: " + "  ".join(
                    f"[{key}] {action}" for key, action in _ACTION_BINDINGS
                ),
                classes="action-hint",
            )
            yield Static("", id="action-result", classes="muted")

    def on_mount(self) -> None:
        self.title = f"LazyServer · {self.entry.name}"
        self.sub_title = self.entry.id
        try:
            view = self.query_one("#files-list", ListView)
        except Exception:
            return
        if view.children:
            view.focus()

    def action_open_focused(self) -> None:
        from .file_screen import FileScreen

        focused = self.focused
        if isinstance(focused, ListView):
            highlighted = focused.highlighted_child
            if isinstance(highlighted, _FileRow):
                self.app.push_screen(
                    FileScreen(self.context, self.entry, highlighted.payload)
                )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        from .file_screen import FileScreen

        if isinstance(event.item, _FileRow):
            self.app.push_screen(
                FileScreen(self.context, self.entry, event.item.payload)
            )

    def action_do_action(self, action_id: str) -> None:
        """Fire the named service action against the resolved entry.

        Reads `self.app.dry_run` live, so the 'd' toggle takes effect
        without re-entering the screen. UnsupportedActionError (e.g.
        pressing 1 on an app entry) is surfaced inline rather than
        crashing the TUI. The inline line records what happened; for
        status and failures the full captured output is also shown in
        a scrollable modal (NFR-5).
        """
        if self.resolved is None or not self.resolved.actions:
            return
        try:
            result_widget = self.query_one("#action-result", Static)
        except Exception:
            return

        try:
            result = execute_action(
                self.resolved, action_id, dry_run=self.app.dry_run
            )
        except UnsupportedActionError as exc:
            result_widget.update(f"⚠ {exc}")
            result_widget.set_class(True, "alert")
            return

        text, is_alert = format_inline_action_summary(
            self.entry.name, action_id, result
        )
        result_widget.update(text)
        result_widget.set_class(is_alert, "alert")

        if should_show_output_modal(action_id, result):
            self.app.push_screen(
                CommandOutputModal(
                    title=f"{self.entry.name} · {action_id}",
                    argv=result.argv,
                    exit_code=result.exit_code,
                    duration_s=result.duration_s,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            )

    @staticmethod
    def _format_file_row(f: ResolvedFile, aliases: tuple[FileAlias, ...]) -> str:
        also_known_as = [
            tuple(other for other in a.file_ids if other != f.id)
            for a in aliases
            if f.id in a.file_ids
        ]
        suffix = ""
        if also_known_as:
            others = also_known_as[0]
            suffix = f"    (also: {', '.join(others)})"
        return f"{f.id}    {f.path}{suffix}"
