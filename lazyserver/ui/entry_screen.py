"""Entry detail — description + managed files list (FR-1.2, FR-4.3)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext
from ..tconf.model import Entry
from ..tconf.resolve import (
    FileAlias,
    ResolutionError,
    ResolvedEntry,
    ResolvedFile,
    ResolvedFileSet,
    resolve,
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

            yield Static(self.context.status_line, id="status-line")
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
