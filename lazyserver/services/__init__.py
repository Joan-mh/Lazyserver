"""Service control layer (architecture §4).

`control.execute_action(resolved, action_id, *, dry_run)` runs the argv
already resolved by `tconf.resolve` (i.e. systemd defaults filled in from
`service_unit`, with any per-entry overrides applied). Dry-run flows
through to the runner: nothing is spawned, but callers still get back a
`RunResult` so the wiring is fully testable (architecture §1, §7).

Action templates themselves live in `tconf/defaults.py` for now — they
are pure data and the resolver needs them. They will likely move here
when service control grows beyond a single function.
"""
