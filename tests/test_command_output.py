"""Routing + inline-summary logic for service-action output (FR-1.5).

The Textual modal itself isn't unit-tested (full widget rendering is
hard and brittle), but the decision functions that drive it are pure
and trivially covered.
"""

from __future__ import annotations

from lazyserver.platform.runner import RunResult
from lazyserver.ui.command_output import (
    format_inline_action_summary,
    should_show_output_modal,
)


def _result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    dry_run: bool = False,
    argv: tuple[str, ...] = ("systemctl", "status", "named"),
) -> RunResult:
    return RunResult(
        argv=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=0.05,
        dry_run=dry_run,
    )


# ---------- should_show_output_modal ----------


def test_status_always_opens_modal_even_when_exit_zero():
    assert should_show_output_modal("status", _result(exit_code=0, stdout="active"))


def test_status_opens_modal_when_exit_nonzero_too():
    """systemctl status returns exit 3 for stopped-but-known units —
    still a 'show me the readout' case, not a failure framing."""
    assert should_show_output_modal("status", _result(exit_code=3, stdout="inactive"))


def test_failure_opens_modal_for_any_action():
    assert should_show_output_modal("restart", _result(exit_code=1, stderr="boom"))
    assert should_show_output_modal("enable", _result(exit_code=1, stderr="boom"))


def test_silent_success_stays_inline():
    assert not should_show_output_modal("restart", _result(exit_code=0))
    assert not should_show_output_modal("enable", _result(exit_code=0))


def test_dry_run_never_opens_modal_even_for_status_or_failure():
    """Dry-run argv has no real output to read; the inline line
    showing the would-be command is enough."""
    assert not should_show_output_modal("status", _result(dry_run=True))
    assert not should_show_output_modal("restart", _result(dry_run=True))


# ---------- format_inline_action_summary ----------


def test_inline_dry_run_shows_full_argv():
    text, is_alert = format_inline_action_summary(
        "BIND9", "restart", _result(dry_run=True, argv=("systemctl", "restart", "named")),
    )
    assert text == "(dry-run) systemctl restart named"
    assert is_alert is False


def test_inline_success_uses_past_tense_per_action():
    cases = {
        "start": "started",
        "stop": "stopped",
        "restart": "restarted",
        "reload": "reloaded",
        "enable": "enabled",
        "disable": "disabled",
    }
    for action_id, verb in cases.items():
        text, is_alert = format_inline_action_summary("BIND9", action_id, _result())
        assert text == f"✓ BIND9 {verb}"
        assert is_alert is False


def test_inline_status_is_neutral_with_exit_code():
    """Status isn't success-or-failure; the readout lives in the modal."""
    text, is_alert = format_inline_action_summary(
        "BIND9", "status", _result(exit_code=3, stdout="inactive"),
    )
    assert text == "▶ BIND9 status — exit 3"
    assert is_alert is False


def test_inline_failure_is_alert_with_exit_code():
    text, is_alert = format_inline_action_summary(
        "BIND9", "restart", _result(exit_code=1, stderr="Unit not found"),
    )
    assert text == "✗ restart failed — exit 1"
    assert is_alert is True


def test_inline_unknown_action_falls_back_gracefully():
    """An entry-defined action like 'reindex' has no past-tense form;
    fall back to '<action> done' instead of failing or being silent."""
    text, _ = format_inline_action_summary("MyApp", "reindex", _result())
    assert text == "✓ MyApp reindex done"
