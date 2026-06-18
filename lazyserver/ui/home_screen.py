"""Home screen — services and apps in separate sections (spec §3, FR-1.1)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ..app import AppContext, format_status_line
from ..tconf.model import Entry


class _EntryItem(ListItem):
    """A list row that carries the Entry it represents."""

    def __init__(self, entry: Entry):
        category = f"  ({entry.category})" if entry.category else ""
        super().__init__(Label(f"{entry.name}{category}"))
        self.entry = entry


class HomeScreen(Screen):
    """Two sections: Services, then Apps. Enter opens an entry detail screen."""

    BINDINGS = [
        Binding("enter", "open_focused", "Open", show=True),
    ]

    def __init__(self, context: AppContext):
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static("Services", classes="section-title")
            yield ListView(
                *(_EntryItem(e) for e in self.context.services),
                id="services-list",
            )
            yield Static("Apps", classes="section-title")
            yield ListView(
                *(_EntryItem(e) for e in self.context.apps),
                id="apps-list",
            )
            yield Static(
                format_status_line(self.context, dry_run=self.app.dry_run),
                id="status-line",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "LazyServer"
        self.sub_title = "browse services and apps"
        # Focus the first non-empty list so arrow keys work out of the box.
        services = self.query_one("#services-list", ListView)
        apps = self.query_one("#apps-list", ListView)
        first = services if services.children else apps
        if first.children:
            first.focus()

    def action_open_focused(self) -> None:
        from .entry_screen import EntryScreen

        focused = self.focused
        if isinstance(focused, ListView):
            highlighted = focused.highlighted_child
            if isinstance(highlighted, _EntryItem):
                self.app.push_screen(EntryScreen(self.context, highlighted.entry))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Mouse click / Enter on a row routes to entry detail."""
        from .entry_screen import EntryScreen

        if isinstance(event.item, _EntryItem):
            self.app.push_screen(EntryScreen(self.context, event.item.entry))
