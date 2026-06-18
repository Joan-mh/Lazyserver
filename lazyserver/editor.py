"""Launch the user's chosen editor against a managed file (FR-1.3, FR-7.3).

The editor is resolved via `config.resolve_editor` and run with
`capture=False` so it inherits the terminal — the TUI must be suspended
around the call. `dry_run=True` short-circuits the launch so flows can
be exercised in tests without a real editor.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings, resolve_editor
from .platform.runner import RunResult, run


def launch_editor(
    settings: Settings,
    path: Path,
    *,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Run `$editor <path>` and wait for it to exit."""
    editor = resolve_editor(settings, env=env)
    return run(
        [editor, str(path)],
        env=env,
        dry_run=dry_run,
        timeout=None,
        capture=False,
    )
