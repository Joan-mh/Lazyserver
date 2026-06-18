"""Dataclasses for a parsed tconf entry (schema §2-§5).

These are *raw* models — the YAML on disk, validated and normalised but not
resolved against any specific distro yet. See `resolve.py` for the per-distro
view used by service control, backup, and recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

KIND_SERVICE = "service"
KIND_APP = "app"
KINDS = (KIND_SERVICE, KIND_APP)

# Action ids the loader will accept under `actions` (schema §5).
STANDARD_ACTIONS = (
    "start",
    "stop",
    "restart",
    "reload",
    "enable",
    "disable",
    "status",
)

DEFAULT_SENTINEL = "default"


@dataclass(frozen=True)
class ManagedFile:
    """A fixed config file declared by an entry (schema §3)."""

    id: str
    description: str
    path: str | None
    example: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class FileSet:
    """A glob-based set of user-created config files (schema §3b)."""

    id: str
    description: str
    pattern: str
    directory: str | None
    example: str | None = None
    optional: bool = True
    owner: str | None = None
    group: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class DistroProfile:
    """Per-distro values inside an entry's `distros:` map (schema §4).

    `actions` keys are action ids; values are either the literal string
    "default" (use the init system's default template) or an argv tuple.
    Bare strings other than "default" are rejected by the loader (schema §5).
    """

    package: str
    install: tuple[str, ...] | None = None
    service_unit: str | None = None
    actions: dict[str, tuple[str, ...] | str] = field(default_factory=dict)
    file_paths: dict[str, str] = field(default_factory=dict)
    file_set_dirs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    """One tconf entry — a service or an application (schema §2)."""

    schema_version: int
    id: str
    name: str
    kind: str
    description: str
    distros: dict[str, DistroProfile]
    files: tuple[ManagedFile, ...] = ()
    file_sets: tuple[FileSet, ...] = ()
    category: str | None = None
    docs_url: str | None = None
    source_path: Path | None = None

    @property
    def is_service(self) -> bool:
        return self.kind == KIND_SERVICE

    @property
    def is_app(self) -> bool:
        return self.kind == KIND_APP
