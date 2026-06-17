import pytest

from lazyserver.platform import runner


def test_dry_run_does_not_execute():
    result = runner.run(["/nope/this/should/never/run", "--bad"], dry_run=True)
    assert result.dry_run is True
    assert result.ok
    assert result.exit_code == 0
    assert result.argv == ("/nope/this/should/never/run", "--bad")
    assert result.stdout == "" and result.stderr == ""


def test_runs_real_command():
    result = runner.run(["true"])
    assert result.dry_run is False
    assert result.ok
    assert result.exit_code == 0


def test_captures_nonzero_exit():
    result = runner.run(["false"])
    assert not result.ok
    assert result.exit_code != 0


def test_captures_stdout():
    result = runner.run(["printf", "hello"])
    assert result.stdout == "hello"


def test_empty_argv_rejected():
    with pytest.raises(ValueError):
        runner.run([])


def test_check_raises_on_failure():
    import subprocess
    with pytest.raises(subprocess.CalledProcessError):
        runner.run(["false"], check=True)
