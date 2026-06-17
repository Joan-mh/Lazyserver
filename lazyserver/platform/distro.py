"""Distro detection from /etc/os-release (architecture §3).

Maps `ID` and `ID_LIKE` from os-release to one of the tconf distro ids:
ubuntu, arch, fedora, opensuse, unknown. Pure function over a path so tests
feed fixture files; no real /etc access in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUPPORTED = ("ubuntu", "arch", "fedora", "opensuse")
UNKNOWN = "unknown"

_ALIASES = {
    "ubuntu": "ubuntu",
    "debian": "ubuntu",
    "arch": "arch",
    "archlinux": "arch",
    "manjaro": "arch",
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "opensuse": "opensuse",
    "opensuse-leap": "opensuse",
    "opensuse-tumbleweed": "opensuse",
    "suse": "opensuse",
    "sles": "opensuse",
}


@dataclass(frozen=True)
class Distro:
    id: str
    pretty_name: str
    raw_id: str
    raw_id_like: tuple[str, ...]


def _parse_os_release(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def _map_to_tconf_id(raw_id: str, raw_id_like: tuple[str, ...]) -> str:
    for candidate in (raw_id, *raw_id_like):
        mapped = _ALIASES.get(candidate.lower())
        if mapped:
            return mapped
    return UNKNOWN


def detect(os_release_path: Path | str = "/etc/os-release") -> Distro:
    """Read os-release and return the resolved Distro.

    Missing or unparsable file → an UNKNOWN Distro with empty fields, never
    an exception. Callers decide how to react to UNKNOWN.
    """
    path = Path(os_release_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Distro(id=UNKNOWN, pretty_name="", raw_id="", raw_id_like=())

    fields = _parse_os_release(text)
    raw_id = fields.get("ID", "")
    raw_id_like = tuple(part for part in fields.get("ID_LIKE", "").split() if part)
    return Distro(
        id=_map_to_tconf_id(raw_id, raw_id_like),
        pretty_name=fields.get("PRETTY_NAME", ""),
        raw_id=raw_id,
        raw_id_like=raw_id_like,
    )
