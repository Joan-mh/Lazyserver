from pathlib import Path

from lazyserver.platform import distro


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "os-release"
    p.write_text(body, encoding="utf-8")
    return p


def test_ubuntu_detection(tmp_path: Path):
    body = (
        'NAME="Ubuntu"\n'
        'VERSION="22.04.4 LTS (Jammy Jellyfish)"\n'
        'ID=ubuntu\n'
        'ID_LIKE=debian\n'
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
    )
    d = distro.detect(_write(tmp_path, body))
    assert d.id == "ubuntu"
    assert d.pretty_name == "Ubuntu 22.04.4 LTS"
    assert d.raw_id == "ubuntu"
    assert d.raw_id_like == ("debian",)
    assert d.inferred is False
    assert d.inference_notice() is None


def test_arch_detection(tmp_path: Path):
    body = (
        'NAME="Arch Linux"\n'
        'PRETTY_NAME="Arch Linux"\n'
        'ID=arch\n'
        'BUILD_ID=rolling\n'
    )
    d = distro.detect(_write(tmp_path, body))
    assert d.id == "arch"
    assert d.raw_id == "arch"
    assert d.raw_id_like == ()
    assert d.inferred is False


def test_manjaro_falls_back_via_id_like(tmp_path: Path):
    body = 'ID=manjaro\nID_LIKE="arch"\nPRETTY_NAME="Manjaro Linux"\n'
    d = distro.detect(_write(tmp_path, body))
    assert d.id == "arch"
    assert d.inferred is True
    notice = d.inference_notice()
    assert notice is not None
    assert "Manjaro" in notice and "arch" in notice


def test_debian_maps_to_ubuntu_family(tmp_path: Path):
    body = 'ID=debian\nPRETTY_NAME="Debian GNU/Linux"\n'
    d = distro.detect(_write(tmp_path, body))
    assert d.id == "ubuntu"
    assert d.inferred is True
    notice = d.inference_notice()
    assert notice is not None
    assert "Debian" in notice and "ubuntu" in notice


def test_unknown_distro(tmp_path: Path):
    body = 'ID=plan9\nPRETTY_NAME="Plan 9"\n'
    d = distro.detect(_write(tmp_path, body))
    assert d.id == "unknown"
    assert d.inferred is False
    assert d.inference_notice() is None


def test_missing_file_returns_unknown(tmp_path: Path):
    d = distro.detect(tmp_path / "does-not-exist")
    assert d.id == "unknown"
    assert d.raw_id == ""
    assert d.inferred is False
