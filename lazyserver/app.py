"""LazyServer Textual application entry (architecture §4).

The CLI invokes :func:`run` (no subcommand) to launch the TUI. This module
also owns the bootstrap that resolves target user, distro, settings, and
loads tconf folders into a single :class:`AppContext` consumed by the
screens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from .config import Settings, default_path, load as load_settings
from .platform.distro import Distro, detect as detect_distro
from .platform.user import TargetUser, TargetUserError
from .platform.user import resolve as resolve_target_user
from .tconf import bundled_tconf_path
from .tconf.loader import LoadReport, load_folders
from .tconf.model import Entry

log = logging.getLogger("lazyserver.app")


class BootstrapError(RuntimeError):
    """A fatal error during startup; the CLI prints and exits non-zero."""


@dataclass(frozen=True)
class AppContext:
    """Everything the TUI needs after startup. Read-only for Phase 2."""

    target_user: TargetUser
    settings: Settings
    distro: Distro
    entries: tuple[Entry, ...]
    shadowed: tuple[tuple[str, Path, Path], ...]

    @property
    def services(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.is_service)

    @property
    def apps(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.is_app)

    @property
    def status_line(self) -> str:
        """One-line summary shown in the footer, including inference warnings."""
        bits = [
            f"distro: {self.distro.id}",
            f"user: {self.target_user.name}",
            f"entries: {len(self.services)}s+{len(self.apps)}a",
        ]
        line = "  ·  ".join(bits)
        notice = self.distro.inference_notice()
        if notice:
            line = f"{line}    ⚠ {notice}"
        return line


def bootstrap() -> AppContext:
    """Resolve user, settings, distro, and tconf folders in that order.

    Order matters: the settings file lives under the target user's home, so
    we need the target user first. Settings then pin tconf_paths and may
    override the target_user (read once more if so), and finally we load
    tconf folders with the bundled defaults prepended.
    """
    try:
        target_user = resolve_target_user()
    except TargetUserError as exc:
        raise BootstrapError(str(exc)) from exc

    settings = load_settings(default_path(target_user))

    if settings.target_user and settings.target_user != target_user.name:
        try:
            target_user = resolve_target_user(override=settings.target_user)
        except TargetUserError as exc:
            raise BootstrapError(str(exc)) from exc

    distro = detect_distro()
    folders = _tconf_folders(settings)
    report = _load_with_clear_errors(folders)

    return AppContext(
        target_user=target_user,
        settings=settings,
        distro=distro,
        entries=tuple(report.entries.values()),
        shadowed=report.shadowed,
    )


def _tconf_folders(settings: Settings) -> list[Path]:
    """Bundled folder always first; user folders shadow per FR-7.4."""
    folders: list[Path] = [bundled_tconf_path()]
    folders.extend(Path(p) for p in settings.tconf_paths)
    return folders


def _load_with_clear_errors(folders: list[Path]) -> LoadReport:
    try:
        return load_folders(folders)
    except Exception as exc:
        raise BootstrapError(f"Failed to load tconf folders: {exc}") from exc


class LazyServerApp(App):
    """Top-level Textual app — pushes the home screen at startup."""

    CSS_PATH = "ui/lazyserver.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "pop_or_quit", "Back", show=True, priority=True),
    ]

    def __init__(self, context: AppContext):
        super().__init__()
        self.context = context

    def on_mount(self) -> None:
        # Import locally to keep `app.py` import-light for non-TUI consumers.
        from .ui.home_screen import HomeScreen

        self.push_screen(HomeScreen(self.context))

    def action_pop_or_quit(self) -> None:
        """Escape from the home screen quits; elsewhere it pops."""
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()


def run() -> int:
    """Launch the TUI. Returns the process exit code."""
    context = bootstrap()
    LazyServerApp(context).run()
    return 0
