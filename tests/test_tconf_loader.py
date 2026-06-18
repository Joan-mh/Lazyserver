from pathlib import Path

import pytest

from lazyserver.tconf import loader
from lazyserver.tconf.loader import ValidationError

MINIMAL_SERVICE = """
schema_version: 1
id: example
name: Example
kind: service
description: |
  Short paragraph describing the service.
files:
  - id: main
    path: /etc/example/main.conf
    description: Main config file.
    example: |
      key = value
distros:
  ubuntu:
    package: example
    service_unit: example
"""


def write(tmp_path: Path, body: str, name: str = "example.yaml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_loads_valid_service(tmp_path: Path):
    path = write(tmp_path, MINIMAL_SERVICE)
    entry = loader.load_file(path)
    assert entry.id == "example"
    assert entry.is_service
    assert entry.files[0].id == "main"
    assert entry.distros["ubuntu"].package == "example"
    assert entry.source_path == path


def test_loads_valid_app(tmp_path: Path):
    body = """
schema_version: 1
id: neovim
name: Neovim
kind: app
description: Modern Vim-based editor.
files:
  - id: init_lua
    path: ~/.config/nvim/init.lua
    description: Main configuration file.
distros:
  ubuntu:
    package: neovim
"""
    entry = loader.load_file(write(tmp_path, body))
    assert entry.is_app
    assert entry.distros["ubuntu"].service_unit is None


def test_missing_schema_version_rejected(tmp_path: Path):
    body = "id: x\nname: X\nkind: service\ndescription: x\ndistros: {ubuntu: {package: x, service_unit: x}}\nfiles: [{id: a, path: /a, description: a}]\n"
    with pytest.raises(ValidationError, match="schema_version"):
        loader.load_file(write(tmp_path, body))


def test_unsupported_schema_version_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace("schema_version: 1", "schema_version: 99")
    with pytest.raises(ValidationError, match="Unsupported schema_version"):
        loader.load_file(write(tmp_path, body))


def test_invalid_kind_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace("kind: service", "kind: widget")
    with pytest.raises(ValidationError, match="kind"):
        loader.load_file(write(tmp_path, body))


def test_service_without_service_unit_rejected(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files: [{id: a, path: /a, description: a}]
distros:
  ubuntu:
    package: x
"""
    with pytest.raises(ValidationError, match="service_unit"):
        loader.load_file(write(tmp_path, body))


def test_app_with_service_unit_rejected(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: app
description: x
files: [{id: a, path: /a, description: a}]
distros:
  ubuntu:
    package: x
    service_unit: x
"""
    with pytest.raises(ValidationError, match="service_unit"):
        loader.load_file(write(tmp_path, body))


def test_no_files_or_file_sets_rejected(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
distros:
  ubuntu:
    package: x
    service_unit: x
"""
    with pytest.raises(ValidationError, match="files.*file_sets"):
        loader.load_file(write(tmp_path, body))


def test_duplicate_file_id_rejected(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files:
  - {id: a, path: /a, description: a}
  - {id: a, path: /b, description: b}
distros:
  ubuntu: {package: x, service_unit: x}
"""
    with pytest.raises(ValidationError, match="Duplicate file id 'a'"):
        loader.load_file(write(tmp_path, body))


def test_file_and_file_set_share_namespace(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files:
  - {id: a, path: /a, description: a}
file_sets:
  - {id: a, directory: /d, pattern: "*.conf", description: d}
distros:
  ubuntu: {package: x, service_unit: x}
"""
    with pytest.raises(ValidationError, match="Duplicate id 'a'"):
        loader.load_file(write(tmp_path, body))


def test_file_with_no_path_and_no_override_rejected(tmp_path: Path):
    body = """
schema_version: 1
id: x
name: X
kind: service
description: x
files:
  - {id: a, description: a}
distros:
  ubuntu: {package: x, service_unit: x, file_paths: {a: /etc/a}}
  arch: {package: x, service_unit: x}
"""
    with pytest.raises(ValidationError, match="no top-level `path`.*arch"):
        loader.load_file(write(tmp_path, body))


def test_file_paths_references_unknown_id_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "service_unit: example",
        "service_unit: example\n    file_paths: {ghost: /etc/ghost.conf}",
    )
    with pytest.raises(ValidationError, match="unknown file id 'ghost'"):
        loader.load_file(write(tmp_path, body))


def test_actions_default_sentinel_accepted(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "service_unit: example",
        "service_unit: example\n    actions:\n      reload: default",
    )
    entry = loader.load_file(write(tmp_path, body))
    assert entry.distros["ubuntu"].actions == {"reload": "default"}


def test_actions_argv_list_accepted(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "service_unit: example",
        'service_unit: example\n    actions:\n      reload: ["/usr/sbin/example", "-s", "reload"]',
    )
    entry = loader.load_file(write(tmp_path, body))
    assert entry.distros["ubuntu"].actions["reload"] == (
        "/usr/sbin/example",
        "-s",
        "reload",
    )


def test_actions_bare_string_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "service_unit: example",
        'service_unit: example\n    actions: {reload: "service example reload"}',
    )
    with pytest.raises(ValidationError, match='bare strings other than "default"'):
        loader.load_file(write(tmp_path, body))


def test_actions_unknown_id_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "service_unit: example",
        "service_unit: example\n    actions: {dance: default}",
    )
    with pytest.raises(ValidationError, match="Unknown action 'dance'"):
        loader.load_file(write(tmp_path, body))


def test_install_argv_accepted(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "package: example",
        'package: example\n    install: ["apt-get", "install", "-y", "example"]',
    )
    entry = loader.load_file(write(tmp_path, body))
    assert entry.distros["ubuntu"].install == (
        "apt-get",
        "install",
        "-y",
        "example",
    )


def test_install_bare_string_rejected(tmp_path: Path):
    body = MINIMAL_SERVICE.replace(
        "package: example",
        'package: example\n    install: "apt-get install -y example"',
    )
    with pytest.raises(ValidationError, match="install must be a list"):
        loader.load_file(write(tmp_path, body))


def test_load_folder_picks_up_services_and_apps(tmp_path: Path):
    (tmp_path / "services").mkdir()
    (tmp_path / "apps").mkdir()
    (tmp_path / "services" / "ex.yaml").write_text(MINIMAL_SERVICE, encoding="utf-8")
    (tmp_path / "apps" / "nv.yaml").write_text(
        MINIMAL_SERVICE.replace("id: example", "id: nv")
        .replace("kind: service", "kind: app")
        .replace("    service_unit: example\n", ""),
        encoding="utf-8",
    )
    entries = loader.load_folder(tmp_path)
    assert {e.id for e in entries} == {"example", "nv"}


def test_load_folder_rejects_duplicate_entry_ids(tmp_path: Path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "a.yaml").write_text(MINIMAL_SERVICE, encoding="utf-8")
    (tmp_path / "services" / "b.yaml").write_text(MINIMAL_SERVICE, encoding="utf-8")
    with pytest.raises(ValidationError, match="Duplicate entry id 'example'"):
        loader.load_folder(tmp_path)


def test_load_folders_last_wins_and_records_shadowed(tmp_path: Path):
    shipped = tmp_path / "shipped" / "services"
    local = tmp_path / "local" / "services"
    shipped.mkdir(parents=True)
    local.mkdir(parents=True)
    (shipped / "ex.yaml").write_text(MINIMAL_SERVICE, encoding="utf-8")
    overridden = MINIMAL_SERVICE.replace("package: example", "package: example-local")
    (local / "ex.yaml").write_text(overridden, encoding="utf-8")
    report = loader.load_folders([shipped.parent, local.parent])
    assert report.entries["example"].distros["ubuntu"].package == "example-local"
    assert len(report.shadowed) == 1
    eid, prev, winner = report.shadowed[0]
    assert eid == "example"
    assert prev.parent == shipped
    assert winner.parent == local


def test_invalid_yaml_reports_path(tmp_path: Path):
    p = write(tmp_path, ":\n:\nnot yaml: [", "bad.yaml")
    with pytest.raises(ValidationError) as exc:
        loader.load_file(p)
    assert str(p) in str(exc.value)
