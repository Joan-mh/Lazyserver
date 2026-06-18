"""Default templates for install and service-control commands (arch §3, §4).

Per NFR-1 these are **data tables**, never `if`/`switch` in code. Adding a
distro or init system means adding a row here, not changing logic.

Templates use `{unit}` and `{package}` placeholders; the resolver substitutes
the entry's `service_unit` and `package` (schema §4 / §5).
"""

from __future__ import annotations

# Which init system applies to each tconf distro id. All MVP distros are
# systemd; adding `fedora`/`opensuse` rows keeps the table honest about
# scope even though they share the value (arch §3, FR-4.2).
INIT_SYSTEM_BY_DISTRO: dict[str, str] = {
    "ubuntu": "systemd",
    "arch": "systemd",
    "fedora": "systemd",
    "opensuse": "systemd",
}

# argv templates per (init_system, action). `{unit}` is replaced with the
# entry's resolved service_unit.
ACTION_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "systemd": {
        "start": ("systemctl", "start", "{unit}"),
        "stop": ("systemctl", "stop", "{unit}"),
        "restart": ("systemctl", "restart", "{unit}"),
        "reload": ("systemctl", "reload", "{unit}"),
        "enable": ("systemctl", "enable", "{unit}"),
        "disable": ("systemctl", "disable", "{unit}"),
        "status": ("systemctl", "status", "{unit}"),
    },
}

# argv templates per distro id for the default install command.
# `{package}` is replaced with the entry's package name. (arch §3.)
INSTALL_TEMPLATES: dict[str, tuple[str, ...]] = {
    "ubuntu": ("apt-get", "install", "-y", "{package}"),
    "arch": ("pacman", "-S", "--noconfirm", "{package}"),
    "fedora": ("dnf", "install", "-y", "{package}"),
    "opensuse": ("zypper", "install", "-y", "{package}"),
}
