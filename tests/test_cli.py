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
