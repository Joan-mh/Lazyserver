"""Full-recovery orchestration (FR-5.3, Phase 6).

Layers:

  * :mod:`plan` — pure planner: turn the resolved entries + cloned
    backup store into a static `RecoveryPlan` (no I/O beyond store
    reads, no subprocess).
  * (orchestrator and report formatters land in later commits)
"""
