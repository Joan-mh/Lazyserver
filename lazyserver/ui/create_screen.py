"""Create-new-file modal (FR-1.7 / FR-1.8 / FR-1.9).

The modal shows the resolved owner/group/mode *before* the user types
the filename so they see why the file will be owned that way. On submit,
the file is created (or planned, under dry-run) and the modal dismisses
with the new path; the caller opens it in the editor.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from ..backup.create import (
    CreateError,
    OwnershipPlan,
    create_file,
    plan_ownership,
)
from ..tconf.model import Entry
from ..tconf.resolve import ResolvedFileSet


class NewFileScreen(ModalScreen[Path]):
    """Permission preview → name input → create + dismiss with the path.

    Dismiss value is the created Path on success, or None on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        entry: Entry,
        file_set: ResolvedFileSet,
        plan: OwnershipPlan,
        *,
        dry_run: bool = False,
    ):
        super().__init__()
        self.entry = entry
        self.file_set = file_set
        self.plan = plan
        self.dry_run = dry_run

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-body"):
            yield Static(
                f"Create new file in '{self.file_set.id}'",
                classes="entry-title",
            )
            yield Static(
                f"directory: {self.file_set.directory}",
                classes="file-path",
            )
            yield Static(
                f"will be created as {self.plan.owner}:{self.plan.group}, "
                f"mode {self.plan.mode}",
                classes="muted",
            )
            yield Static(f"reason: {self.plan.reason}", classes="muted")
            if self.plan.is_fallback_root:
                yield Static(
                    "⚠ root-owned fallback — the service may be unable to "
                    "read this file. Consider chowning after create.",
                    classes="alert",
                )
            yield Input(
                placeholder="filename (no slashes, no ..)",
                id="filename-input",
            )
            yield Static("", id="create-error", classes="alert")
            if self.file_set.example:
                yield Static("Will be pre-filled with the example:", classes="muted")
                with VerticalScroll(classes="example-scroll"):
                    yield Static(self.file_set.example.rstrip(), classes="example")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#filename-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        err = self.query_one("#create-error", Static)
        if not name:
            err.update("filename required")
            return
        if "/" in name or name in {".", ".."} or name.startswith(".."):
            err.update("filename must be a plain name (no path separators or ..)")
            return
        target = Path(self.file_set.directory) / name
        content = self.file_set.example or ""
        try:
            create_file(target, content=content, plan=self.plan, dry_run=self.dry_run)
        except CreateError as exc:
            err.update(str(exc))
            return
        except PermissionError as exc:
            err.update(f"permission denied: {exc}")
            return
        self.dismiss(target)
