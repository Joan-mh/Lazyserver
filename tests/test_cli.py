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


def test_no_command_exits_zero(capsys):
    code = cli.main([])
    assert code == 0
    assert "not implemented" in capsys.readouterr().err.lower()


def test_module_entry_runs_version():
    result = subprocess.run(
        [sys.executable, "-m", "lazyserver.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in result.stdout
