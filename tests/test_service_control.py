"""Service control — wiring + dry-run coverage (Phase 3b).

The key property the user asked us to confirm: every service-control
action can be exercised with `dry_run=True` so the **resolved argv** is
testable without firing a real `systemctl`. These tests load shipped
entries, resolve them per distro, and assert the exact argv each action
would dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lazyserver.platform.user import TargetUser
from lazyserver.services.control import (
    UnsupportedActionError,
    execute_action,
)
from lazyserver.tconf import bundled_tconf_path, loader
from lazyserver.tconf.resolve import resolve

FAKE_USER = TargetUser(name="alice", uid=1000, gid=1000, home=Path("/home/alice"))


def _entry(eid: str):
    entries = {e.id: e for e in loader.load_folder(bundled_tconf_path())}
    return entries[eid]


@pytest.mark.parametrize("distro_id,expected_unit", [("ubuntu", "named"), ("arch", "named")])
def test_bind9_start_dry_run(distro_id, expected_unit):
    r = resolve(_entry("bind9"), distro_id)
    result = execute_action(r, "start", dry_run=True)
    assert result.dry_run is True
    assert result.exit_code == 0
    assert result.argv == ("systemctl", "start", expected_unit)


@pytest.mark.parametrize(
    "action", ["start", "stop", "restart", "reload", "enable", "disable", "status"]
)
def test_every_destructive_and_safe_action_resolves_under_dry_run(action):
    """All 7 standard actions wire through end-to-end under dry-run, no
    confirmation, no subprocess."""
    r = resolve(_entry("nginx"), "ubuntu")
    result = execute_action(r, action, dry_run=True)
    assert result.dry_run is True
    assert result.argv == ("systemctl", action, "nginx")


def test_distro_difference_visible_in_argv():
    """Apache illustrates the per-distro unit name difference: apache2 vs httpd."""
    ubuntu = execute_action(resolve(_entry("apache"), "ubuntu"), "restart", dry_run=True)
    arch = execute_action(resolve(_entry("apache"), "arch"), "restart", dry_run=True)
    assert ubuntu.argv == ("systemctl", "restart", "apache2")
    assert arch.argv == ("systemctl", "restart", "httpd")


def test_unknown_action_raises():
    r = resolve(_entry("bind9"), "ubuntu")
    with pytest.raises(UnsupportedActionError, match="dance"):
        execute_action(r, "dance", dry_run=True)


def test_app_entry_has_no_actions():
    """Neovim is an app — no service_unit, no resolved actions. Asking to
    start it must fail loudly, not silently no-op."""
    r = resolve(_entry("neovim"), "ubuntu", target_user=FAKE_USER)
    assert r.actions == {}
    with pytest.raises(UnsupportedActionError, match="known actions:"):
        execute_action(r, "start", dry_run=True)


def test_entry_override_propagates_through_control(tmp_path):
    """A custom reload argv defined in the entry survives all the way to
    the runner under dry-run."""
    body = """
schema_version: 1
id: custom
name: Custom
kind: service
description: x
files: [{id: a, path: /etc/a, description: a}]
distros:
  ubuntu:
    package: custom
    service_unit: custom
    actions:
      reload: ["/usr/sbin/custom", "-s", "reload"]
"""
    p = tmp_path / "custom.yaml"
    p.write_text(body, encoding="utf-8")
    entry = loader.load_file(p)
    r = resolve(entry, "ubuntu")
    result = execute_action(r, "reload", dry_run=True)
    assert result.argv == ("/usr/sbin/custom", "-s", "reload")
    # Defaults for untouched actions still flow through.
    result = execute_action(r, "start", dry_run=True)
    assert result.argv == ("systemctl", "start", "custom")
