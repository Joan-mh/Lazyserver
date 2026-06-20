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
    DEFAULT_INSTALL_TIMEOUT_S,
    NoInstallCommandError,
    UnsupportedActionError,
    execute_action,
    execute_install,
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
    "action,expected_argv",
    [
        ("start", ("systemctl", "start", "nginx")),
        ("stop", ("systemctl", "stop", "nginx")),
        ("restart", ("systemctl", "restart", "nginx")),
        ("reload", ("systemctl", "reload", "nginx")),
        ("enable", ("systemctl", "enable", "nginx")),
        ("disable", ("systemctl", "disable", "nginx")),
        # status carries --no-pager so captured stdout is the full
        # text rather than a less-driven empty buffer.
        ("status", ("systemctl", "--no-pager", "status", "nginx")),
    ],
)
def test_every_destructive_and_safe_action_resolves_under_dry_run(action, expected_argv):
    """All 7 standard actions wire through end-to-end under dry-run, no
    confirmation, no subprocess."""
    r = resolve(_entry("nginx"), "ubuntu")
    result = execute_action(r, action, dry_run=True)
    assert result.dry_run is True
    assert result.argv == expected_argv


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


# ---------- execute_install (Phase 6) ----------


def test_install_dry_run_uses_resolved_argv_for_ubuntu():
    """Phase 6: the resolved install argv (from the per-distro template
    plus the entry's package name) flows through dry-run end-to-end.
    Same dry-run guarantee as execute_action — apt-get never runs."""
    r = resolve(_entry("bind9"), "ubuntu")
    result = execute_install(r, dry_run=True)
    assert result.dry_run is True
    assert result.exit_code == 0
    assert result.argv == ("apt-get", "install", "-y", "bind9")


def test_install_dry_run_uses_resolved_argv_for_arch():
    r = resolve(_entry("bind9"), "arch")
    result = execute_install(r, dry_run=True)
    assert result.dry_run is True
    assert result.argv == ("pacman", "-S", "--noconfirm", "bind")


def test_install_distro_difference_visible_in_argv():
    """Apache: ubuntu installs `apache2`, arch installs `apache`. The
    install step is per-distro just like the service actions."""
    ubuntu = execute_install(resolve(_entry("apache"), "ubuntu"), dry_run=True)
    arch = execute_install(resolve(_entry("apache"), "arch"), dry_run=True)
    assert ubuntu.argv == ("apt-get", "install", "-y", "apache2")
    assert arch.argv == ("pacman", "-S", "--noconfirm", "apache")


def test_install_raises_when_no_install_argv(monkeypatch):
    """Defensive: if the resolver produces an empty install (or a future
    surface bypasses the planner gate), execute_install fails loudly
    instead of silently no-op'ing during recovery."""
    r = resolve(_entry("bind9"), "ubuntu")
    # Replace the immutable install field via dataclasses.replace.
    from dataclasses import replace as dc_replace

    blank = dc_replace(r, install=())
    with pytest.raises(NoInstallCommandError, match="bind9"):
        execute_install(blank, dry_run=True)


def test_install_default_timeout_is_long_enough_for_apt(monkeypatch):
    """Documented behaviour: the install default is 10 minutes, not
    the 30s used for service actions. Locking the constant prevents
    a refactor from silently dropping it back to the action default
    and breaking slow-link installs in a VM."""
    assert DEFAULT_INSTALL_TIMEOUT_S == 600.0

    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = tuple(argv)
        seen["timeout"] = kwargs.get("timeout")
        from lazyserver.platform.runner import RunResult

        return RunResult(
            argv=tuple(argv),
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.0,
            dry_run=kwargs.get("dry_run", False),
        )

    monkeypatch.setattr("lazyserver.services.control.run", fake_run)
    r = resolve(_entry("bind9"), "ubuntu")
    execute_install(r, dry_run=True)
    assert seen["timeout"] == DEFAULT_INSTALL_TIMEOUT_S


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
