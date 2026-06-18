"""Execute a resolved service action (FR-1.5).

The resolver has already produced an argv per action for the active
distro, so this module is a thin wrapper that:

- looks up the requested action on the resolved entry,
- raises a clear, actionable error if it is not available
  (e.g. asking to `start` an app, or an action the loader-validated
  override map does not include), and
- forwards to `platform.runner.run`, honoring `dry_run` end-to-end.

No confirmation prompts (NFR-2, spec §9 Deployment assumptions).
"""

from __future__ import annotations

import logging

from ..platform.runner import RunResult, run
from ..tconf.resolve import ResolvedEntry

log = logging.getLogger("lazyserver.services.control")

DEFAULT_TIMEOUT_S = 30.0


class UnsupportedActionError(LookupError):
    """The requested action id has no resolved argv on this entry/distro."""


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
