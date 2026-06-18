"""File / file-set detail — description + example (FR-4.3)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ..app import AppContext
from ..tconf.model import Entry
from ..tconf.resolve import ResolvedFile, ResolvedFileSet


class FileScreen(Screen):
    """One managed file (fixed or set) — purpose, location, example."""

    BINDINGS = [
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
            yield Static(self.context.status_line, id="status-line")
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

    def on_mount(self) -> None:
        title = self.payload.id
        self.title = f"LazyServer · {self.entry.name}"
        self.sub_title = title
