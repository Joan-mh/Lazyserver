"""Per-distro resolution of a tconf Entry (schema §4-§5, arch §3-§4).

Given an Entry and a target distro id, produce a ResolvedEntry with effective
file paths, file_set directories, install argv, and the full action argv
table. App file paths beginning with `~` are expanded against the target
user's home (FR-1.10, schema §10).

Also exposes `expand_file_set()` — the canonical glob expansion used by
both the TUI and the backup scanner (FR-1.6). Globs are *not* cached;
each call walks the filesystem, so files created after an entry was
defined are picked up immediately.
"""

from __future__ import annotations

import glob as _glob
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
class FileAlias:
    """Group of fixed-file ids that all resolve to one path on this distro.

    Surfaced so backup/edit/modification-detection can dedupe (one file on
    disk = one checksum, one snapshot), and so the TUI can explain to the
    student why two declared files map to the same target — the bind9-on-arch
    case where named.conf.options and named.conf.local are split on Debian
    but combined into /etc/named.conf on Arch. No schema change required.
    """

    path: str
    file_ids: tuple[str, ...]
    note: str


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
    aliases: tuple[FileAlias, ...] = ()


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
    aliases = _detect_file_aliases(entry, distro_id, files)

    return ResolvedEntry(
        entry=entry,
        distro_id=distro_id,
        package=profile.package,
        service_unit=profile.service_unit,
        install=install,
        actions=actions,
        files=files,
        file_sets=file_sets,
        aliases=aliases,
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


def _detect_file_aliases(
    entry: Entry, distro_id: str, files: tuple[ResolvedFile, ...]
) -> tuple[FileAlias, ...]:
    """Group fixed files that resolved to the same path on `distro_id`.

    Scope: fixed `files` only — `file_sets` are excluded (their semantics
    are directory+glob, and a directory collision is a different concern).
    Each group carries a didactic note that, when possible, contrasts with
    a sibling distro where the same ids resolve to *different* paths.
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for f in files:
        grouped[f.path].append(f.id)

    aliases: list[FileAlias] = []
    for path, ids in grouped.items():
        if len(ids) < 2:
            continue
        ids_sorted = tuple(sorted(ids))
        aliases.append(
            FileAlias(
                path=path,
                file_ids=ids_sorted,
                note=_build_alias_note(entry, distro_id, ids_sorted, path),
            )
        )
    return tuple(sorted(aliases, key=lambda a: a.path))


def _build_alias_note(
    entry: Entry, distro_id: str, group_ids: tuple[str, ...], shared_path: str
) -> str:
    quantifier = "both" if len(group_ids) == 2 else "all"
    ids_phrase = _and_join(group_ids)
    base = (
        f"On {distro_id}, {ids_phrase} {quantifier} resolve to {shared_path}; "
        "LazyServer treats them as one file (single edit, single backup)."
    )
    contrast = _find_contrast_distro(entry, distro_id, group_ids)
    if contrast is None:
        return base
    other_id, other_paths = contrast
    pairs = ", ".join(
        f"{fid} → {path}" for fid, path in zip(group_ids, other_paths)
    )
    return f"{base} On {other_id} they are separate: {pairs}."


def _find_contrast_distro(
    entry: Entry, distro_id: str, group_ids: tuple[str, ...]
) -> tuple[str, tuple[str, ...]] | None:
    """Find another distro where the same ids resolve to >1 distinct paths."""
    for other_id in entry.distros:
        if other_id == distro_id:
            continue
        paths = tuple(_file_path_on(entry, fid, other_id) for fid in group_ids)
        if any(p is None for p in paths):
            continue
        if len(set(paths)) > 1:
            return other_id, paths  # type: ignore[return-value]
    return None


def _file_path_on(entry: Entry, file_id: str, distro_id: str) -> str | None:
    """Return the un-expanded resolved path of file_id on distro_id.

    Used only to build the didactic contrast in alias notes — no `~`
    expansion, no error raising; returns None when the lookup is impossible.
    """
    profile = entry.distros.get(distro_id)
    if profile is None:
        return None
    f = next((f for f in entry.files if f.id == file_id), None)
    if f is None:
        return None
    return profile.file_paths.get(f.id, f.path)


def _and_join(items: tuple[str, ...]) -> str:
    if len(items) == 1:
        return repr(items[0])
    if len(items) == 2:
        return f"{items[0]!r} and {items[1]!r}"
    return ", ".join(repr(x) for x in items[:-1]) + f", and {items[-1]!r}"


def expand_file_set(fs: ResolvedFileSet) -> list[Path]:
    """Glob a file_set against the current filesystem state (FR-1.6).

    Returns sorted absolute paths, files only. `**` is honored only if
    the user wrote it in the pattern (schema §3b). An absent directory
    yields the empty list — the set is just not yet populated, not an
    error.

    Called fresh on every backup scan and every TUI refresh so files
    created after the entry was defined are caught (the live-VM bug fix
    in FileScreen + the FR-1.6 backup-time expansion both rely on this).
    """
    base = Path(fs.directory)
    if not base.is_dir():
        return []
    recursive = "**" in fs.pattern
    matches = _glob.glob(str(base / fs.pattern), recursive=recursive)
    return sorted(Path(m) for m in matches if Path(m).is_file())


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
