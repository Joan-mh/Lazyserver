"""Smoke test: the shipped tconf files load and resolve (FR-4.2)."""

from pathlib import Path

import pytest

from lazyserver.platform.user import TargetUser
from lazyserver.tconf import bundled_tconf_path, loader
from lazyserver.tconf.resolve import resolve

EXPECTED_SERVICES = {
    "bind9",
    "isc-dhcp-server",
    "vsftpd",
    "squid",
    "nginx",
    "apache",
    "postfix",
    "dovecot",
}

EXPECTED_APPS = {"neovim"}

FAKE_USER = TargetUser(
    name="alice", uid=1000, gid=1000, home=Path("/home/alice")
)


def test_all_shipped_entries_load():
    entries = loader.load_folder(bundled_tconf_path())
    ids = {e.id for e in entries}
    assert EXPECTED_SERVICES.issubset(ids), (
        f"Missing services: {EXPECTED_SERVICES - ids}"
    )
    assert EXPECTED_APPS.issubset(ids), f"Missing apps: {EXPECTED_APPS - ids}"


@pytest.mark.parametrize("distro_id", ["ubuntu", "arch"])
def test_all_shipped_entries_resolve(distro_id: str):
    entries = loader.load_folder(bundled_tconf_path())
    for entry in entries:
        if distro_id not in entry.distros:
            pytest.fail(
                f"Shipped entry {entry.id!r} has no profile for {distro_id!r}."
            )
        r = resolve(entry, distro_id, target_user=FAKE_USER)
        # Sanity: every resolved file has an absolute path; every file_set
        # has an absolute directory.
        for f in r.files:
            assert f.path.startswith("/"), (
                f"{entry.id}:{f.id} on {distro_id} resolved to {f.path!r}"
            )
        for fs in r.file_sets:
            assert fs.directory.startswith("/"), (
                f"{entry.id}:{fs.id} on {distro_id} resolved to {fs.directory!r}"
            )
        # Services must have a service_unit and at least the start template.
        if entry.is_service:
            assert r.service_unit, f"{entry.id} on {distro_id} missing service_unit"
            assert r.actions["start"][0] == "systemctl"
        # Install command must be filled in.
        assert r.install, f"{entry.id} on {distro_id} has empty install argv"
