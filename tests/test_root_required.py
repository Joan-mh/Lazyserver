"""NFR-3 — LazyServer refuses to start unless euid == 0.

We don't actually run as root in CI; we exercise the check by injecting
geteuid. Real-world boot still goes through bootstrap() which calls
check_root_privilege() with the real os.geteuid.
"""

from __future__ import annotations

import pytest

from lazyserver.app import BootstrapError, check_root_privilege


def test_non_root_raises_bootstrap_error():
    with pytest.raises(BootstrapError) as excinfo:
        check_root_privilege(geteuid=lambda: 1000)
    msg = str(excinfo.value)
    assert "sudo" in msg.lower()
    assert "lazyserver" in msg.lower()


def test_root_passes():
    # Must not raise.
    check_root_privilege(geteuid=lambda: 0)


def test_bootstrap_fails_clean_when_not_root(monkeypatch):
    """End-to-end: cli.main([]) under non-root exits 1 with a useful
    stderr line — no traceback."""
    import sys

    from lazyserver import cli

    # Force non-root and intercept run() so we see the real bootstrap call.
    from lazyserver import app as app_module

    monkeypatch.setattr(app_module.os, "geteuid", lambda: 1000)

    rc = cli.main([])
    assert rc == 1
    # Stderr is captured by capsys in cli tests; here we just check the
    # exit code path — the CLI prints `lazyserver: <msg>` and returns 1.
