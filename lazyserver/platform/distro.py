"""Distro detection from /etc/os-release (architecture §3).

Maps `ID` and `ID_LIKE` from os-release to one of the tconf distro ids:
ubuntu, arch, fedora, opensuse, unknown. Pure function over a path so tests
feed fixture files; no real /etc access in tests.

Per arch §3: an exact `ID` match against a supported tconf id (ubuntu, arch,
fedora, opensuse) is treated as supported. Any other resolution — via an
alias of a different name (debian→ubuntu, manjaro→arch) or via `ID_LIKE` —
maps the same way but is flagged as inferred so the UI can warn the user
that paths and package names are usually-but-not-always identical.
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
    inferred: bool = False

    def inference_notice(self) -> str | None:
        """User-facing notice when the tconf id was inferred (arch §3).

        Returns None when the match was exact (or unknown). Phrased for the
        TUI/CLI to display once at startup; the didactic point is to tell
        the student what we inferred.
        """
        if not self.inferred or self.id == UNKNOWN:
            return None
        source = self.pretty_name or self.raw_id or "this system"
        return (
            f"Detected {source}; treating it as {self.id} — package names "
            "and paths are usually but not always identical."
        )


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


def _map_to_tconf_id(raw_id: str, raw_id_like: tuple[str, ...]) -> tuple[str, bool]:
    """Return (tconf_id, inferred).

    Exact ID match against a SUPPORTED id → not inferred. Alias of a
    different name, or any ID_LIKE fallback, → inferred.
    """
    lowered = raw_id.lower()
    if lowered in SUPPORTED:
        return lowered, False
    if lowered in _ALIASES:
        return _ALIASES[lowered], True
    for candidate in raw_id_like:
        mapped = _ALIASES.get(candidate.lower())
        if mapped:
            return mapped, True
    return UNKNOWN, False


def detect(os_release_path: Path | str = "/etc/os-release") -> Distro:
    """Read os-release and return the resolved Distro.

    Missing or unparsable file → an UNKNOWN Distro with empty fields, never
    an exception. Callers decide how to react to UNKNOWN.
    """
    path = Path(os_release_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Distro(
            id=UNKNOWN, pretty_name="", raw_id="", raw_id_like=(), inferred=False
        )

    fields = _parse_os_release(text)
    raw_id = fields.get("ID", "")
    raw_id_like = tuple(part for part in fields.get("ID_LIKE", "").split() if part)
    tconf_id, inferred = _map_to_tconf_id(raw_id, raw_id_like)
    return Distro(
        id=tconf_id,
        pretty_name=fields.get("PRETTY_NAME", ""),
        raw_id=raw_id,
        raw_id_like=raw_id_like,
        inferred=inferred,
    )
