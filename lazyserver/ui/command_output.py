"""Scrollable modal for a single command's captured output (FR-1.5, NFR-5).

Where this fits: pressing a service-action key on EntryScreen always
records a one-line inline summary; *additionally*, for status and any
failed action we pop this modal so the student can read the full
captured stdout/stderr. The runner already captures both — this is
purely a presentation layer.

Routing decision lives in ``should_show_output_modal`` so the rule is
unit-testable without spinning up Textual; inline text comes from
``format_inline_action_summary`` for the same reason.

Rule (from the user's framing):
  * status → always modal (it's the whole reason status exists)
  * exit != 0 → always modal (the error text is what lets a student debug)
  * silent success → inline only
  * dry-run → inline only (no real output to read)
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from ..platform.runner import RunResult

_ACTION_PAST_TENSE: dict[str, str] = {
    "start": "started",
    "stop": "stopped",
    "restart": "restarted",
    "reload": "reloaded",
    "enable": "enabled",
    "disable": "disabled",
}


def should_show_output_modal(action_id: str, result: RunResult) -> bool:
    """True iff the captured output deserves a focused, scrollable view."""
    if result.dry_run:
        return False
    if action_id == "status":
        return True
    return not result.ok


def format_inline_action_summary(
    entry_name: str, action_id: str, result: RunResult
) -> tuple[str, bool]:
    """Return ``(text, is_alert)`` for the EntryScreen inline result line.

    Kept brief: it's both the only feedback for silent successes and
    the persistent record after a modal is dismissed. The modal itself
    carries the full output, so this line never needs to.
    """
    if result.dry_run:
        return f"(dry-run) {' '.join(result.argv)}", False
    if action_id == "status":
        # Neutral framing — status isn't success-or-failure, it's a
        # readout. Exit code goes on the line for completeness;
        # interpretation lives in the modal text.
        return f"▶ {entry_name} status — exit {result.exit_code}", False
    if result.ok:
        verb = _ACTION_PAST_TENSE.get(action_id, f"{action_id} done")
        return f"✓ {entry_name} {verb}", False
    return f"✗ {action_id} failed — exit {result.exit_code}", True


class CommandOutputModal(ModalScreen[None]):
    """Read-only, scrollable view of one RunResult's captured output."""

    BINDINGS = [
        Binding("escape,enter,q", "close", "Close", show=True),
    ]

    def __init__(
        self,
        *,
        title: str,
        argv: tuple[str, ...],
        exit_code: int,
        duration_s: float,
        stdout: str,
        stderr: str,
    ):
        super().__init__()
        self._title = title
        self._argv = argv
        self._exit_code = exit_code
        self._duration_s = duration_s
        self._stdout = stdout
        self._stderr = stderr

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-body"):
            yield Static(self._title, classes="entry-title")
            yield Static(f"$ {' '.join(self._argv)}", classes="muted")
            yield Static(
                f"exit {self._exit_code} · {self._duration_s:.2f}s",
                classes="muted",
            )
            with VerticalScroll(classes="command-output-scroll"):
                stdout = self._stdout.rstrip("\n")
                stderr = self._stderr.rstrip("\n")
                if stdout:
                    yield Static("─── stdout ───", classes="section-title")
                    yield Static(stdout, classes="command-output-text")
                if stderr:
                    yield Static("─── stderr ───", classes="section-title")
                    yield Static(stderr, classes="command-output-text")
                if not stdout and not stderr:
                    yield Static("(no output)", classes="muted")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)
