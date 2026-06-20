"""Execute a resolved service action (FR-1.5) — and install (FR-5.3.1).

The resolver has already produced an argv per action for the active
distro, so this module is a thin wrapper that:

- looks up the requested action on the resolved entry,
- raises a clear, actionable error if it is not available
  (e.g. asking to `start` an app, or an action the loader-validated
  override map does not include), and
- forwards to `platform.runner.run`, honoring `dry_run` end-to-end.

`execute_install` is the Phase-6 sibling for the recovery flow: same
shape, different field on the resolved entry (`install` is its own
slot, not part of the `actions` map) and a much longer default timeout
because `apt-get install` / `pacman -S` over a slow link is normal.

No confirmation prompts (NFR-2, spec §9 Deployment assumptions).
"""

from __future__ import annotations

import logging

from ..platform.runner import RunResult, run
from ..tconf.resolve import ResolvedEntry

log = logging.getLogger("lazyserver.services.control")

DEFAULT_TIMEOUT_S = 30.0
# Install commands fetch packages over the network; 10 minutes is the
# default upper bound an unattended `lsrv recover --all` will tolerate
# per step before treating it as a stall. Callers can override.
DEFAULT_INSTALL_TIMEOUT_S = 600.0


class UnsupportedActionError(LookupError):
    """The requested action id has no resolved argv on this entry/distro."""


class NoInstallCommandError(LookupError):
    """The entry resolved without an install argv on this distro.

    Raised by `execute_install` when called on an entry that the
    planner would have marked install-not-applicable. The recovery
    orchestrator gates this upstream; the raise is defensive so a
    direct caller (test, future surface) cannot silently no-op.
    """


def execute_action(
    resolved: ResolvedEntry,
    action_id: str,
    *,
    dry_run: bool = False,
    timeout: float | None = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Run `action_id` on `resolved`.

    Returns the RunResult as captured by the runner (including stdout,
    stderr, exit code). With `dry_run=True` no process is spawned;
    `RunResult.dry_run` is True and `exit_code` is 0, so calling code can
    walk through the same branches as a successful real run.
    """
    argv = resolved.actions.get(action_id)
    if not argv:
        raise UnsupportedActionError(
            f"Action {action_id!r} is not available for "
            f"{resolved.entry.id!r} on {resolved.distro_id!r}; "
            f"known actions: {sorted(resolved.actions)}"
        )
    log.info(
        "service control: %s.%s (dry_run=%s) argv=%s",
        resolved.entry.id,
        action_id,
        dry_run,
        argv,
    )
    return run(list(argv), dry_run=dry_run, timeout=timeout)


def execute_install(
    resolved: ResolvedEntry,
    *,
    dry_run: bool = False,
    timeout: float | None = DEFAULT_INSTALL_TIMEOUT_S,
) -> RunResult:
    """Run the resolved install command for `resolved`.

    `resolved.install` is the argv prepared by the resolver — either an
    entry-supplied override or the per-distro default template (e.g.
    `apt-get install -y bind9`). With `dry_run=True` no process is
    spawned; `RunResult.dry_run` is True and `exit_code` is 0, mirroring
    `execute_action`.

    Raises `NoInstallCommandError` if the entry resolved without an
    install argv — the recovery orchestrator should consult the
    planner's `InstallStep.applicable` before calling.
    """
    argv = resolved.install
    if not argv:
        raise NoInstallCommandError(
            f"Entry {resolved.entry.id!r} has no install command for "
            f"distro {resolved.distro_id!r}; check the planner's "
            "InstallStep.applicable before calling execute_install."
        )
    log.info(
        "service control: %s.install (dry_run=%s) argv=%s",
        resolved.entry.id,
        dry_run,
        argv,
    )
    return run(list(argv), dry_run=dry_run, timeout=timeout)
