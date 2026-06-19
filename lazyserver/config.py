"""User settings persistence (FR-7).

TOML at the target user's XDG config dir, owned by the target user. Read via
stdlib `tomllib`; written by a tiny emitter because the schema is small and
flat. If `editor` is unset, callers use `resolve_editor()` to pick one from
$VISUAL → $EDITOR → nano → vi (FR-7.3).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .platform.user import TargetUser

CONFIG_DIRNAME = "lazyserver"
CONFIG_FILENAME = "config.toml"

EDITOR_FALLBACKS = ("nano", "vi")


class ConfigError(ValueError):
    """Settings file could not be loaded or parsed (FR-7, NFR-5).

    Raised so bootstrap can surface a clean stderr message instead of
    letting a raw TOMLDecodeError traceback hit the student. Carries
    the user-facing message in ``str(exc)``.
    """


@dataclass(frozen=True)
class Settings:
    editor: str | None = None
    backup_store: str | None = None
    tconf_paths: tuple[str, ...] = field(default_factory=tuple)
    target_user: str | None = None


def default_path(user: TargetUser) -> Path:
    """Path to the settings file for `user` (FR-7.2).

    Always `~target/.config/lazyserver/config.toml`. A custom
    `$XDG_CONFIG_HOME` set in the target user's shell is intentionally
    ignored: under sudo we run with root's environment, so the target user's
    env (and therefore their XDG override) is not available to us. Honouring
    the env we *do* see would put config under root's XDG_CONFIG_HOME, which
    is worse — the file would not be owned by, nor visible to, the target
    user on the next non-sudo invocation. Documented limitation (spec FR-7.2).
    """
    return user.home / ".config" / CONFIG_DIRNAME / CONFIG_FILENAME


def load(path: Path) -> Settings:
    """Load settings from `path`. Missing file → defaults.

    A malformed TOML file raises ``ConfigError`` with a user-facing
    message: the file path and the parser's description (which on
    Python 3.11+ includes the line/column). The recovery hint tells
    students they can delete the file to start fresh — important
    because they otherwise can't launch the app to fix it from the
    TUI.
    """
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"failed to parse {path}: {exc}\n"
            "Fix the TOML syntax, or delete the file to start fresh "
            "with defaults."
        ) from exc
    return Settings(
        editor=_as_str(data.get("editor")),
        backup_store=_as_str(data.get("backup_store")),
        tconf_paths=tuple(_as_str_list(data.get("tconf_paths"))),
        target_user=_as_str(data.get("target_user")),
    )


def save(settings: Settings, path: Path, *, owner: TargetUser | None = None) -> None:
    """Atomically write `settings` to `path`, owned by `owner` if given.

    Creates the parent directory if missing. If `owner` is provided and we
    have privilege, chowns the file and parent to the target user so a
    root-run session does not leave root-owned config behind (FR-7.2).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_emit_toml(settings), encoding="utf-8")
    os.replace(tmp, path)

    if owner is not None:
        _try_chown(path.parent, owner)
        _try_chown(path, owner)


def resolve_editor(settings: Settings, env: dict[str, str] | None = None) -> str:
    """Return the editor to launch (FR-7.3).

    Order: explicit settings.editor → $VISUAL → $EDITOR → nano → vi (first
    that exists on PATH). Returns the final fallback name even if it cannot
    be found, so callers always get *something* to try.
    """
    environ = env if env is not None else os.environ
    candidates = [
        settings.editor,
        environ.get("VISUAL"),
        environ.get("EDITOR"),
        *EDITOR_FALLBACKS,
    ]
    for cand in candidates:
        if cand and shutil.which(cand):
            return cand
    return EDITOR_FALLBACKS[-1]


def with_target_user(settings: Settings, name: str | None) -> Settings:
    """Return a copy with `target_user` set; used by the CLI override hook."""
    return replace(settings, target_user=name)


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected string, got {type(value).__name__}")
    return value


def _as_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected list of strings")
    return list(value)


def _try_chown(path: Path, owner: TargetUser) -> None:
    try:
        os.chown(path, owner.uid, owner.gid)
    except PermissionError:
        pass


def _emit_toml(settings: Settings) -> str:
    lines: list[str] = []
    if settings.editor is not None:
        lines.append(f"editor = {_toml_string(settings.editor)}")
    if settings.backup_store is not None:
        lines.append(f"backup_store = {_toml_string(settings.backup_store)}")
    if settings.target_user is not None:
        lines.append(f"target_user = {_toml_string(settings.target_user)}")
    if settings.tconf_paths:
        items = ", ".join(_toml_string(p) for p in settings.tconf_paths)
        lines.append(f"tconf_paths = [{items}]")
    else:
        lines.append("tconf_paths = []")
    lines.append("")
    return "\n".join(lines)


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toml_string(value: str) -> str:
    return '"' + "".join(_TOML_ESCAPES.get(c, c) for c in value) + '"'
