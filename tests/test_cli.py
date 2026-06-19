import subprocess
import sys

import pytest

from lazyserver import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_no_command_launches_tui(monkeypatch):
    """cli.main([]) calls into the Textual app; we mock the actual run."""
    from lazyserver import app as app_module

    captured: dict = {}

    def fake_run(*, dry_run: bool = False) -> int:
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(app_module, "run", fake_run)
    assert cli.main([]) == 0
    assert captured == {"dry_run": False}


def test_dry_run_flag_propagates(monkeypatch):
    from lazyserver import app as app_module

    captured: dict = {}

    def fake_run(*, dry_run: bool = False) -> int:
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(app_module, "run", fake_run)
    assert cli.main(["--dry-run"]) == 0
    assert captured == {"dry_run": True}


def test_no_command_reports_bootstrap_failure(capsys, monkeypatch):
    from lazyserver import app as app_module

    def fake_run(*, dry_run: bool = False) -> int:
        raise app_module.BootstrapError("synthetic boom")

    monkeypatch.setattr(app_module, "run", fake_run)
    assert cli.main([]) == 1
    assert "synthetic boom" in capsys.readouterr().err


def test_module_entry_runs_version():
    result = subprocess.run(
        [sys.executable, "-m", "lazyserver.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in result.stdout


def test_malformed_config_toml_exits_cleanly(tmp_path, capsys, monkeypatch):
    """A TOML syntax error in config.toml must surface as a clean
    stderr line + exit 1, never as a raw traceback (NFR-5)."""
    from pathlib import Path

    from lazyserver import app as app_module
    from lazyserver.platform.distro import Distro
    from lazyserver.platform.user import TargetUser

    bad = tmp_path / "config.toml"
    bad.write_text('editor = "unterminated\n', encoding="utf-8")
    user = TargetUser(name="alice", uid=1000, gid=1000, home=tmp_path)

    monkeypatch.setattr(app_module, "check_root_privilege", lambda: None)
    monkeypatch.setattr(app_module, "resolve_target_user", lambda *a, **kw: user)
    monkeypatch.setattr(app_module, "default_path", lambda u: bad)
    # Stub distro detection — irrelevant past this point because we
    # never reach it (settings load fails first).
    monkeypatch.setattr(
        app_module,
        "detect_distro",
        lambda: Distro(id="ubuntu", pretty_name="Ubuntu", raw_id="ubuntu", raw_id_like=(), inferred=False),
    )

    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert str(bad) in err
    assert "delete" in err.lower()
    # No raw exception class names leaking through.
    assert "Traceback" not in err
    assert "TOMLDecodeError" not in err
