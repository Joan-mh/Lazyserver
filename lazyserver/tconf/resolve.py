"""Per-distro resolution of a tconf Entry (schema §4-§5, arch §3-§4).

Given an Entry and a target distro id, produce a ResolvedEntry with effective
file paths, file_set directories, install argv, and the full action argv
table. App file paths beginning with `~` are expanded against the target
user's home (FR-1.10, schema §10).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ..platform.user import TargetUser
from . import defaults
from .model import (
    DEFAULT_SENTINEL,
    KIND_APP,
    Entry,
    FileSet,
    ManagedFile,
)


class ResolutionError(ValueError):
    """Raised when an Entry cannot be resolved for the requested distro."""


@dataclass(frozen=True)
class ResolvedFile:
    id: str
    path: str
    description: str
    example: str | None
    optional: bool


@dataclass(frozen=True)
class ResolvedFileSet:
    id: str
    directory: str
    pattern: str
    description: str
    example: str | None
    optional: bool
    owner: str | None
    group: str | None
    mode: str | None


@dataclass(frozen=True)
class ResolvedEntry:
    entry: Entry
    distro_id: str
    package: str
    service_unit: str | None
    install: tuple[str, ...]
    actions: dict[str, tuple[str, ...]]
    files: tuple[ResolvedFile, ...]
    file_sets: tuple[ResolvedFileSet, ...]


def resolve(
    entry: Entry,
    distro_id: str,
    *,
    target_user: TargetUser | None = None,
) -> ResolvedEntry:
    """Resolve `entry` for `distro_id`.

    `target_user` is only consulted to expand `~` in app file paths (FR-1.10);
    services use system paths and ignore it. Passing None for an app entry
    that uses `~` raises ResolutionError.
    """
    profile = entry.distros.get(distro_id)
    if profile is None:
        raise ResolutionError(
            f"Entry {entry.id!r} has no profile for distro {distro_id!r}; "
            f"available: {sorted(entry.distros)}"
        )

    files = tuple(
        _resolve_file(entry, f, profile.file_paths, target_user) for f in entry.files
    )
    file_sets = tuple(
        _resolve_file_set(entry, fs, profile.file_set_dirs, target_user)
        for fs in entry.file_sets
    )
    install = _resolve_install(profile, distro_id)
    actions = _resolve_actions(profile, distro_id)

    return ResolvedEntry(
        entry=entry,
        distro_id=distro_id,
        package=profile.package,
        service_unit=profile.service_unit,
        install=install,
        actions=actions,
        files=files,
        file_sets=file_sets,
    )


def _resolve_file(
    entry: Entry,
    f: ManagedFile,
    overrides: dict[str, str],
    target_user: TargetUser | None,
) -> ResolvedFile:
    raw = overrides.get(f.id, f.path)
    if not raw:
        # Loader normally catches this; defence-in-depth for hand-built entries.
        raise ResolutionError(
            f"File {entry.id}:{f.id} has no path (set top-level `path` or "
            "add it under the distro's `file_paths`)."
        )
    return ResolvedFile(
        id=f.id,
        path=_expand_user(raw, entry, target_user),
        description=f.description,
        example=f.example,
        optional=f.optional,
    )


def _resolve_file_set(
    entry: Entry,
    fs: FileSet,
    overrides: dict[str, str],
    target_user: TargetUser | None,
) -> ResolvedFileSet:
    raw = overrides.get(fs.id, fs.directory)
    if not raw:
        raise ResolutionError(
            f"File set {entry.id}:{fs.id} has no directory (set top-level "
            "`directory` or add it under the distro's `file_set_dirs`)."
        )
    return ResolvedFileSet(
        id=fs.id,
        directory=_expand_user(raw, entry, target_user),
        pattern=fs.pattern,
        description=fs.description,
        example=fs.example,
        optional=fs.optional,
        owner=fs.owner,
        group=fs.group,
        mode=fs.mode,
    )


def _resolve_install(profile, distro_id: str) -> tuple[str, ...]:
    if profile.install is not None:
        return profile.install
    template = defaults.INSTALL_TEMPLATES.get(distro_id)
    if template is None:
        raise ResolutionError(
            f"No default install template for distro {distro_id!r}; either "
            "add a row in tconf/defaults.py or supply `install` in the entry."
        )
    return tuple(_subst(arg, package=profile.package) for arg in template)


def _resolve_actions(profile, distro_id: str) -> dict[str, tuple[str, ...]]:
    init = defaults.INIT_SYSTEM_BY_DISTRO.get(distro_id)
    template_table = defaults.ACTION_TEMPLATES.get(init or "", {})

    resolved: dict[str, tuple[str, ...]] = {}
    # Start from the init system's defaults (only meaningful when a unit name exists).
    if profile.service_unit:
        for action, template in template_table.items():
            resolved[action] = tuple(
                _subst(arg, unit=profile.service_unit) for arg in template
            )

    # Apply entry overrides on top.
    for action, value in profile.actions.items():
        if value == DEFAULT_SENTINEL:
            if action not in resolved:
                raise ResolutionError(
                    f"Action {action!r} requested `default` but no template "
                    f"exists for distro {distro_id!r} (init={init!r}). Either "
                    "give an explicit argv or set `service_unit`."
                )
            continue  # keep the template value
        # The loader has already validated value is a tuple of strings.
        resolved[action] = tuple(value)  # type: ignore[arg-type]

    return resolved


def _subst(arg: str, **subs: str) -> str:
    out = arg
    for key, value in subs.items():
        out = out.replace("{" + key + "}", value)
    return out


def _expand_user(
    raw: str, entry: Entry, target_user: TargetUser | None
) -> str:
    if not raw.startswith("~"):
        return raw
    if entry.kind != KIND_APP:
        # Schema says ~ expansion is for app entries; for services it's a
        # configuration error, surfaced loudly per NFR-5.
        raise ResolutionError(
            f"Service entry {entry.id!r} uses `~` in path {raw!r}; only "
            "app entries (kind: app) may use ~."
        )
    if target_user is None:
        raise ResolutionError(
            f"App entry {entry.id!r} uses `~` in path {raw!r}; resolve() "
            "needs `target_user=` to expand it."
        )
    rest = raw[1:]
    if rest.startswith("/"):
        rest = rest[1:]
    return str(PurePosixPath(str(target_user.home)) / rest)
