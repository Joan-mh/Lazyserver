from pathlib import Path

import pytest

from lazyserver.platform.user import TargetUser
from lazyserver.tconf import loader
from lazyserver.tconf.resolve import ResolutionError, resolve


SERVICE_WITH_OVERRIDES = """
schema_version: 1
id: ex
name: Example
kind: service
description: x
files:
  - {id: top, path: /etc/top.conf, description: top}
  - {id: per_distro, description: per-distro path}
file_sets:
  - {id: dropins, directory: /etc/ex.d, pattern: "*.conf", description: drop-ins}
distros:
  ubuntu:
    package: example
    service_unit: ex
    file_paths: {per_distro: /etc/ubuntu/ex.conf}
  arch:
    package: example
    service_unit: ex
    file_paths: {per_distro: /etc/arch/ex.conf}
    file_set_dirs: {dropins: /etc/ex/conf.d}
"""


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ex.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_resolves_top_level_path_when_no_override(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    r = resolve(entry, "ubuntu")
    by_id = {f.id: f for f in r.files}
    assert by_id["top"].path == "/etc/top.conf"


def test_resolves_per_distro_override(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    r_ubuntu = resolve(entry, "ubuntu")
    r_arch = resolve(entry, "arch")
    assert {f.id: f.path for f in r_ubuntu.files}["per_distro"] == "/etc/ubuntu/ex.conf"
    assert {f.id: f.path for f in r_arch.files}["per_distro"] == "/etc/arch/ex.conf"


def test_resolves_file_set_directory_override(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    r_ubuntu = resolve(entry, "ubuntu")
    r_arch = resolve(entry, "arch")
    assert r_ubuntu.file_sets[0].directory == "/etc/ex.d"
    assert r_arch.file_sets[0].directory == "/etc/ex/conf.d"


def test_actions_filled_from_systemd_defaults(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    r = resolve(entry, "ubuntu")
    assert r.actions["start"] == ("systemctl", "start", "ex")
    assert r.actions["reload"] == ("systemctl", "reload", "ex")
    # `enable_now` is the systemd "enable for autostart + start" composite
    # consumed by recovery (FR-5.3.1); see tconf/defaults.py.
    assert set(r.actions) == {
        "start", "stop", "restart", "reload",
        "enable", "disable", "status", "enable_now",
    }
    assert r.actions["enable_now"] == ("systemctl", "enable", "--now", "ex")


def test_action_override_replaces_template(tmp_path: Path):
    body = SERVICE_WITH_OVERRIDES.replace(
        "service_unit: ex\n    file_paths: {per_distro: /etc/ubuntu/ex.conf}",
        "service_unit: ex\n    file_paths: {per_distro: /etc/ubuntu/ex.conf}\n"
        '    actions:\n      reload: ["/usr/sbin/ex", "-s", "reload"]',
    )
    entry = loader.load_file(write(tmp_path, body))
    r = resolve(entry, "ubuntu")
    assert r.actions["reload"] == ("/usr/sbin/ex", "-s", "reload")
    assert r.actions["start"] == ("systemctl", "start", "ex")  # untouched


def test_install_template_substituted(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    assert resolve(entry, "ubuntu").install == (
        "apt-get", "install", "-y", "example",
    )
    assert resolve(entry, "arch").install == (
        "pacman", "-S", "--noconfirm", "example",
    )


def test_explicit_install_overrides_template(tmp_path: Path):
    body = SERVICE_WITH_OVERRIDES.replace(
        "package: example\n    service_unit: ex\n    file_paths: {per_distro: /etc/ubuntu/ex.conf}",
        'package: example\n    install: ["custom-installer", "example"]\n'
        "    service_unit: ex\n    file_paths: {per_distro: /etc/ubuntu/ex.conf}",
    )
    entry = loader.load_file(write(tmp_path, body))
    assert resolve(entry, "ubuntu").install == ("custom-installer", "example")


def test_unknown_distro_raises(tmp_path: Path):
    entry = loader.load_file(write(tmp_path, SERVICE_WITH_OVERRIDES))
    with pytest.raises(ResolutionError, match="no profile for distro 'fedora'"):
        resolve(entry, "fedora")


def test_app_tilde_expansion(tmp_path: Path):
    body = """
schema_version: 1
id: nv
name: Neovim
kind: app
description: x
files:
  - {id: init, path: ~/.config/nvim/init.lua, description: init}
distros:
  ubuntu: {package: neovim}
"""
    entry = loader.load_file(write(tmp_path, body))
    user = TargetUser(name="alice", uid=1000, gid=1000, home=Path("/home/alice"))
    r = resolve(entry, "ubuntu", target_user=user)
    assert r.files[0].path == "/home/alice/.config/nvim/init.lua"


def test_app_tilde_without_target_user_raises(tmp_path: Path):
    body = """
schema_version: 1
id: nv
name: Neovim
kind: app
description: x
files:
  - {id: init, path: ~/.config/nvim/init.lua, description: init}
distros:
  ubuntu: {package: neovim}
"""
    entry = loader.load_file(write(tmp_path, body))
    with pytest.raises(ResolutionError, match="needs `target_user="):
        resolve(entry, "ubuntu")


def test_bind9_on_arch_collapses_named_conf_files(tmp_path: Path):
    """Real-world motivation: Arch ships a single /etc/named.conf, so the
    two declared bind9 files resolve to the same path and must be treated
    as one (single backup, single edit). The note contrasts with ubuntu."""
    from lazyserver.tconf import bundled_tconf_path
    entries = {e.id: e for e in loader.load_folder(bundled_tconf_path())}
    r = resolve(entries["bind9"], "arch")
    assert len(r.aliases) == 1
    alias = r.aliases[0]
    assert alias.path == "/etc/named.conf"
    assert set(alias.file_ids) == {"named_conf_options", "named_conf_local"}
    assert "ubuntu" in alias.note
    assert "named.conf.options" in alias.note
    assert "named.conf.local" in alias.note


def test_bind9_on_ubuntu_has_no_aliases(tmp_path: Path):
    from lazyserver.tconf import bundled_tconf_path
    entries = {e.id: e for e in loader.load_folder(bundled_tconf_path())}
    r = resolve(entries["bind9"], "ubuntu")
    assert r.aliases == ()


def test_synthetic_two_id_collision(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files:
  - {id: a, description: a}
  - {id: b, description: b}
distros:
  ubuntu:
    package: x
    service_unit: x
    file_paths: {a: /etc/shared.conf, b: /etc/shared.conf}
"""
    entry = loader.load_file(write(tmp_path, body))
    r = resolve(entry, "ubuntu")
    assert len(r.aliases) == 1
    alias = r.aliases[0]
    assert alias.path == "/etc/shared.conf"
    assert alias.file_ids == ("a", "b")
    # Only one distro defined → no contrast clause appended.
    assert "they are separate" not in alias.note
    assert "/etc/shared.conf" in alias.note


def test_service_with_tilde_raises(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files:
  - {id: a, path: ~/etc/a, description: a}
distros:
  ubuntu: {package: x, service_unit: x}
"""
    entry = loader.load_file(write(tmp_path, body))
    user = TargetUser(name="alice", uid=1000, gid=1000, home=Path("/home/alice"))
    with pytest.raises(ResolutionError, match="only.*app entries"):
        resolve(entry, "ubuntu", target_user=user)
