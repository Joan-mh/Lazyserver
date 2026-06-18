"""YAML loader + validator for tconf entries (schema §2-§5, §8).

Reads one YAML file per entry; folders contain many. Across folders, the
later path in `tconf_paths` wins by entry id (spec FR-7.4). Within a single
folder, duplicate ids are an error (schema §8).

Validation is loud and specific: every rejection points at the file and the
offending field, per NFR-5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .model import (
    DEFAULT_SENTINEL,
    KIND_APP,
    KIND_SERVICE,
    KINDS,
    STANDARD_ACTIONS,
    DistroProfile,
    Entry,
    FileSet,
    ManagedFile,
)

log = logging.getLogger("lazyserver.tconf.loader")

SUPPORTED_SCHEMA_VERSIONS = (1,)


class TconfError(ValueError):
    """Base error from the tconf loader."""


class ValidationError(TconfError):
    """A YAML file failed schema validation."""

    def __init__(self, message: str, *, path: Path | None = None):
        if path is not None:
            message = f"{path}: {message}"
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class LoadReport:
    """Outcome of loading one or more folders.

    `entries` is the final id→Entry map after last-wins resolution. `shadowed`
    lists (entry_id, shadowed_path, winner_path) tuples so the UI can show
    the user which local overrides shadowed which shipped entry (FR-7.4).
    """

    entries: dict[str, Entry]
    shadowed: tuple[tuple[str, Path, Path], ...]


def load_file(path: Path | str) -> Entry:
    """Read and validate a single tconf YAML file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise TconfError(f"Cannot read {p}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML: {exc}", path=p) from exc
    if not isinstance(raw, dict):
        raise ValidationError("Top-level YAML must be a mapping.", path=p)
    return _build_entry(raw, source_path=p)


def load_folder(folder: Path | str) -> list[Entry]:
    """Load every *.yaml under `folder/services` and `folder/apps`.

    Subfolders matching the spec layout are read; loose YAML at the folder
    root is also accepted (kind comes from the file itself, not the
    directory). Within `folder`, duplicate entry ids raise ValidationError.
    """
    root = Path(folder)
    if not root.exists():
        raise TconfError(f"tconf folder does not exist: {root}")
    if not root.is_dir():
        raise TconfError(f"tconf path is not a directory: {root}")

    files = sorted(_iter_yaml_files(root))
    entries: dict[str, Entry] = {}
    for file_path in files:
        entry = load_file(file_path)
        if entry.id in entries:
            prior = entries[entry.id].source_path
            raise ValidationError(
                f"Duplicate entry id {entry.id!r} in {root}: also defined in {prior}.",
                path=file_path,
            )
        entries[entry.id] = entry
    return list(entries.values())


def load_folders(folders: Iterable[Path | str]) -> LoadReport:
    """Load multiple folders with last-wins semantics on entry id (FR-7.4)."""
    merged: dict[str, Entry] = {}
    shadowed: list[tuple[str, Path, Path]] = []
    for folder in folders:
        for entry in load_folder(folder):
            if entry.id in merged:
                prev = merged[entry.id]
                if prev.source_path and entry.source_path:
                    shadowed.append((entry.id, prev.source_path, entry.source_path))
                    log.info(
                        "tconf: %r in %s shadows %s",
                        entry.id,
                        entry.source_path,
                        prev.source_path,
                    )
            merged[entry.id] = entry
    return LoadReport(entries=merged, shadowed=tuple(shadowed))


def _iter_yaml_files(root: Path) -> Iterable[Path]:
    for sub in ("services", "apps"):
        d = root / sub
        if d.is_dir():
            yield from d.glob("*.yaml")
            yield from d.glob("*.yml")
    yield from root.glob("*.yaml")
    yield from root.glob("*.yml")


def _build_entry(raw: dict, *, source_path: Path) -> Entry:
    _require(raw, "schema_version", int, source_path)
    version = raw["schema_version"]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValidationError(
            f"Unsupported schema_version {version!r}; supported: "
            f"{list(SUPPORTED_SCHEMA_VERSIONS)}.",
            path=source_path,
        )

    entry_id = _require(raw, "id", str, source_path).strip()
    if not entry_id:
        raise ValidationError("`id` must be a non-empty string.", path=source_path)

    name = _require(raw, "name", str, source_path)
    kind = _require(raw, "kind", str, source_path)
    if kind not in KINDS:
        raise ValidationError(
            f"`kind` must be one of {list(KINDS)}, got {kind!r}.", path=source_path
        )
    description = _require(raw, "description", str, source_path)

    distros_raw = raw.get("distros")
    if not isinstance(distros_raw, dict) or not distros_raw:
        raise ValidationError(
            "`distros` is required and must be a non-empty mapping.",
            path=source_path,
        )

    files_raw = raw.get("files") or []
    file_sets_raw = raw.get("file_sets") or []
    if not files_raw and not file_sets_raw:
        raise ValidationError(
            "Entry must declare at least one of `files` or `file_sets`.",
            path=source_path,
        )

    files = tuple(_build_file(item, entry_id, source_path) for item in files_raw)
    file_sets = tuple(
        _build_file_set(item, entry_id, source_path) for item in file_sets_raw
    )

    _check_id_namespace(entry_id, files, file_sets, source_path)

    file_ids = {f.id for f in files}
    file_set_ids = {fs.id for fs in file_sets}

    distros = {
        distro_id: _build_profile(
            distro_id, prof, kind, file_ids, file_set_ids, source_path
        )
        for distro_id, prof in distros_raw.items()
    }

    _check_paths_resolvable(files, file_sets, distros, source_path)

    return Entry(
        schema_version=version,
        id=entry_id,
        name=name,
        kind=kind,
        category=_optional_str(raw.get("category"), "category", source_path),
        description=description,
        docs_url=_optional_str(raw.get("docs_url"), "docs_url", source_path),
        files=files,
        file_sets=file_sets,
        distros=distros,
        source_path=source_path,
    )


def _build_file(item: object, entry_id: str, source_path: Path) -> ManagedFile:
    if not isinstance(item, dict):
        raise ValidationError(
            f"`files` items must be mappings; got {type(item).__name__}.",
            path=source_path,
        )
    fid = _require(item, "id", str, source_path, where=f"files entry of {entry_id!r}")
    description = _require(
        item, "description", str, source_path, where=f"files[{fid!r}]"
    )
    return ManagedFile(
        id=fid,
        description=description,
        path=_optional_str(item.get("path"), f"files[{fid!r}].path", source_path),
        example=_optional_str(
            item.get("example"), f"files[{fid!r}].example", source_path
        ),
        optional=_optional_bool(
            item.get("optional"), f"files[{fid!r}].optional", source_path, default=False
        ),
    )


def _build_file_set(item: object, entry_id: str, source_path: Path) -> FileSet:
    if not isinstance(item, dict):
        raise ValidationError(
            f"`file_sets` items must be mappings; got {type(item).__name__}.",
            path=source_path,
        )
    fid = _require(
        item, "id", str, source_path, where=f"file_sets entry of {entry_id!r}"
    )
    description = _require(
        item, "description", str, source_path, where=f"file_sets[{fid!r}]"
    )
    pattern = _require(
        item, "pattern", str, source_path, where=f"file_sets[{fid!r}]"
    )
    return FileSet(
        id=fid,
        description=description,
        pattern=pattern,
        directory=_optional_str(
            item.get("directory"), f"file_sets[{fid!r}].directory", source_path
        ),
        example=_optional_str(
            item.get("example"), f"file_sets[{fid!r}].example", source_path
        ),
        optional=_optional_bool(
            item.get("optional"),
            f"file_sets[{fid!r}].optional",
            source_path,
            default=True,
        ),
        owner=_optional_str(
            item.get("owner"), f"file_sets[{fid!r}].owner", source_path
        ),
        group=_optional_str(
            item.get("group"), f"file_sets[{fid!r}].group", source_path
        ),
        mode=_optional_str(
            item.get("mode"), f"file_sets[{fid!r}].mode", source_path
        ),
    )


def _check_id_namespace(
    entry_id: str,
    files: tuple[ManagedFile, ...],
    file_sets: tuple[FileSet, ...],
    source_path: Path,
) -> None:
    """Files and file_sets share one flat id namespace per entry (schema §8)."""
    seen: dict[str, str] = {}
    for f in files:
        if f.id in seen:
            raise ValidationError(
                f"Duplicate file id {f.id!r} within entry {entry_id!r} "
                f"(also used by {seen[f.id]}).",
                path=source_path,
            )
        seen[f.id] = "files"
    for fs in file_sets:
        if fs.id in seen:
            raise ValidationError(
                f"Duplicate id {fs.id!r} within entry {entry_id!r}: file_set "
                f"shares the namespace with `files` (also used by {seen[fs.id]}).",
                path=source_path,
            )
        seen[fs.id] = "file_sets"


def _build_profile(
    distro_id: str,
    prof: object,
    kind: str,
    file_ids: set[str],
    file_set_ids: set[str],
    source_path: Path,
) -> DistroProfile:
    if not isinstance(prof, dict):
        raise ValidationError(
            f"`distros.{distro_id}` must be a mapping; got {type(prof).__name__}.",
            path=source_path,
        )
    package = _require(
        prof, "package", str, source_path, where=f"distros.{distro_id}"
    )

    service_unit = _optional_str(
        prof.get("service_unit"), f"distros.{distro_id}.service_unit", source_path
    )
    if kind == KIND_SERVICE and not service_unit:
        raise ValidationError(
            f"Service entry needs `service_unit` in distros.{distro_id}.",
            path=source_path,
        )
    if kind == KIND_APP and service_unit:
        raise ValidationError(
            f"App entry must not set `service_unit` (got {service_unit!r} in "
            f"distros.{distro_id}); apps are not daemons.",
            path=source_path,
        )

    install = _build_install(
        prof.get("install"), distro_id=distro_id, source_path=source_path
    )
    actions = _build_actions(
        prof.get("actions"),
        kind=kind,
        distro_id=distro_id,
        source_path=source_path,
    )
    file_paths = _build_override_map(
        prof.get("file_paths"),
        valid_ids=file_ids,
        kind_label="file",
        field_label=f"distros.{distro_id}.file_paths",
        source_path=source_path,
    )
    file_set_dirs = _build_override_map(
        prof.get("file_set_dirs"),
        valid_ids=file_set_ids,
        kind_label="file_set",
        field_label=f"distros.{distro_id}.file_set_dirs",
        source_path=source_path,
    )

    return DistroProfile(
        package=package,
        install=install,
        service_unit=service_unit,
        actions=actions,
        file_paths=file_paths,
        file_set_dirs=file_set_dirs,
    )


def _build_install(
    raw: object, *, distro_id: str, source_path: Path
) -> tuple[str, ...] | None:
    """Accept None (use default) or argv list. Bare strings are rejected
    so we never reach for a shell (NFR-1, mirrors schema §5 actions rule)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not raw or not all(isinstance(item, str) for item in raw):
            raise ValidationError(
                f"distros.{distro_id}.install must be a non-empty list of strings.",
                path=source_path,
            )
        return tuple(raw)
    raise ValidationError(
        f"distros.{distro_id}.install must be a list (argv), not a string — "
        "schema §5 forbids implicit shell parsing.",
        path=source_path,
    )


def _build_actions(
    raw: object, *, kind: str, distro_id: str, source_path: Path
) -> dict[str, tuple[str, ...] | str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError(
            f"distros.{distro_id}.actions must be a mapping.", path=source_path
        )
    if kind == KIND_APP and raw:
        raise ValidationError(
            f"App entry must not declare `actions` (distros.{distro_id}); "
            "apps have no service-control actions.",
            path=source_path,
        )
    out: dict[str, tuple[str, ...] | str] = {}
    for action_id, value in raw.items():
        if action_id not in STANDARD_ACTIONS:
            raise ValidationError(
                f"Unknown action {action_id!r} in distros.{distro_id}.actions; "
                f"valid: {list(STANDARD_ACTIONS)}.",
                path=source_path,
            )
        if value == DEFAULT_SENTINEL:
            out[action_id] = DEFAULT_SENTINEL
            continue
        if isinstance(value, list) and all(isinstance(arg, str) for arg in value):
            if not value:
                raise ValidationError(
                    f"distros.{distro_id}.actions.{action_id} argv is empty.",
                    path=source_path,
                )
            out[action_id] = tuple(value)
            continue
        if isinstance(value, str):
            raise ValidationError(
                f"distros.{distro_id}.actions.{action_id}: bare strings other "
                'than "default" are rejected — give an argv list (schema §5).',
                path=source_path,
            )
        raise ValidationError(
            f"distros.{distro_id}.actions.{action_id} must be \"default\" or "
            "a list of strings.",
            path=source_path,
        )
    return out


def _build_override_map(
    raw: object,
    *,
    valid_ids: set[str],
    kind_label: str,
    field_label: str,
    source_path: Path,
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError(
            f"{field_label} must be a mapping.", path=source_path
        )
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError(
                f"{field_label} keys and values must be strings.",
                path=source_path,
            )
        if key not in valid_ids:
            raise ValidationError(
                f"{field_label} references unknown {kind_label} id {key!r}; "
                f"known: {sorted(valid_ids)}.",
                path=source_path,
            )
        out[key] = value
    return out


def _check_paths_resolvable(
    files: tuple[ManagedFile, ...],
    file_sets: tuple[FileSet, ...],
    distros: dict[str, DistroProfile],
    source_path: Path,
) -> None:
    """Every file needs a path either at top level or in every distro block
    that does not provide it (schema §8). Same for file_set directories."""
    for f in files:
        if f.path:
            continue
        for distro_id, prof in distros.items():
            if f.id not in prof.file_paths:
                raise ValidationError(
                    f"File {f.id!r} has no top-level `path` and no override "
                    f"in distros.{distro_id}.file_paths.",
                    path=source_path,
                )
    for fs in file_sets:
        if fs.directory:
            continue
        for distro_id, prof in distros.items():
            if fs.id not in prof.file_set_dirs:
                raise ValidationError(
                    f"File set {fs.id!r} has no top-level `directory` and no "
                    f"override in distros.{distro_id}.file_set_dirs.",
                    path=source_path,
                )


# ---------- small helpers ----------


def _require(
    obj: dict,
    key: str,
    expected_type: type,
    source_path: Path,
    *,
    where: str | None = None,
) -> object:
    if key not in obj:
        loc = f" in {where}" if where else ""
        raise ValidationError(
            f"Missing required field `{key}`{loc}.", path=source_path
        )
    value = obj[key]
    if not isinstance(value, expected_type):
        loc = f" in {where}" if where else ""
        raise ValidationError(
            f"`{key}`{loc} must be {expected_type.__name__}, got "
            f"{type(value).__name__}.",
            path=source_path,
        )
    return value


def _optional_str(value: object, field: str, source_path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"`{field}` must be a string, got {type(value).__name__}.",
            path=source_path,
        )
    return value


def _optional_bool(
    value: object, field: str, source_path: Path, *, default: bool
) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(
            f"`{field}` must be a boolean, got {type(value).__name__}.",
            path=source_path,
        )
    return value
