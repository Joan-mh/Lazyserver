"""Recovery report — data shape + pure formatters (FR-5.3.2, FR-5.3.3).

Two artifacts per recovery run, produced from the same in-memory
report:

  * **Human log** — what happened, in the order it happened, with the
    tail of stderr inlined under any failure so the operator can read
    one file and understand the run.
  * **JSON summary** — the same information in a stable, schema-versioned
    shape suitable for scripts, CI and the future v2 Ansible exporter.

The formatters are *pure* — they take a fully-populated `RecoveryReport`
and return a string / dict. Writing to disk and computing artifact
paths live alongside them but are equally side-effect-light: path
computation is pure, and the writer is a thin caller that the
orchestrator (Phase 6 step 4) and the CLI (step 5) drive.

**Stderr tail in failures.** Both artifacts include the last
``STDERR_TAIL_LINES`` lines of stderr for failed steps. Joan's
decision 5: a script reading the JSON should not have to also parse
the human log to learn *why* a step failed. The tail keeps payload
small (10 lines covers an apt error or a systemd failure summary)
while preserving the actionable signal.

**Dry-run shape is the same.** Joan's decision 7: the dry-run path
writes the JSON too, with ``"dry_run": true`` at the top level and
per-step ``status: "would-run"``. One code path, scripts get a plan
they can diff against the post-run JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

JSON_SCHEMA_VERSION = 1

# How many trailing lines of stderr to include in failure reports. 10
# is enough for the standard apt / pacman / systemctl failure summary
# without blowing up the artifact size on a noisy stack trace.
STDERR_TAIL_LINES = 10

# Step status values. Kept as plain strings (not Enum) so the JSON
# round-trips without a custom encoder and so cross-language consumers
# of the artifact do not need a code table.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
# Underscore (not hyphen) so JSON consumers can use attribute-style
# access (``obj.would_run`` in JS, ``d["would_run"]`` in Python) and
# the key reads as a single identifier alongside the other statuses.
# Display tags ("WOULD-RUN", "would-run" in the human log) stay
# hyphenated because they're English-readable, not consumer-parsed.
STATUS_WOULD_RUN = "would_run"

# Entry status values; superset of step statuses with `partial` for
# "some applicable steps OK, some failed".
ENTRY_OK = "ok"
ENTRY_PARTIAL = "partial"
ENTRY_FAILED = "failed"
ENTRY_SKIPPED = "skipped"
ENTRY_WOULD_RUN = "would_run"

STEP_NAMES = ("install", "restore", "enable")


# ---------- data shape (produced by the orchestrator) ----------


@dataclass(frozen=True)
class StepResult:
    """Outcome of one step (install/restore/enable) for one entry.

    Heterogeneous on purpose: install/enable carry argv + exit code +
    duration; restore carries snapshot + file counts. Fields not
    relevant to a given step stay None — the formatters branch on
    `name`. A single dataclass beats three near-identical ones.
    """

    name: str
    status: str
    argv: tuple[str, ...] | None = None
    exit_code: int | None = None
    duration_s: float | None = None
    stderr_tail: str | None = None
    reason: str | None = None
    # restore-only:
    snapshot: str | None = None
    files_restored: int | None = None
    files_failed: int | None = None
    extras_reported: int | None = None


@dataclass(frozen=True)
class EntryResult:
    """All step results for one entry, plus a roll-up status.

    `status` is derived from the steps by `derive_entry_status` — kept
    on the dataclass so the JSON shape is self-describing (a script
    consumer does not have to re-derive it).
    """

    entry_id: str
    entry_name: str
    is_service: bool
    status: str
    steps: tuple[StepResult, ...]


@dataclass(frozen=True)
class RecoveryReport:
    """Everything the formatters need; produced by the orchestrator."""

    timestamp: str
    distro_id: str
    ordering_note: str
    dry_run: bool
    entries: tuple[EntryResult, ...] = field(default_factory=tuple)


# ---------- entry-status derivation (pure, tested) ----------


def derive_entry_status(steps: Iterable[StepResult]) -> str:
    """Roll step statuses up to one per-entry status.

    Rules (mirrors the cascade design Joan signed off on):

      * Any step `would-run`  → entry is `would-run` (dry-run plan).
      * All steps `skipped`   → entry is `skipped` (nothing applicable).
      * Any step `failed` and no step `ok` → entry is `failed` (the
        install root cause + cascade case).
      * Any step `failed` and at least one step `ok` → `partial`.
      * Otherwise (only `ok` + `skipped` present) → `ok`.

    Non-applicable steps surface as `skipped`; they do not count
    against `ok` because the plan didn't intend to run them.
    """
    statuses = [s.status for s in steps]
    if any(s == STATUS_WOULD_RUN for s in statuses):
        return ENTRY_WOULD_RUN
    if all(s == STATUS_SKIPPED for s in statuses):
        return ENTRY_SKIPPED
    has_failed = any(s == STATUS_FAILED for s in statuses)
    has_ok = any(s == STATUS_OK for s in statuses)
    if has_failed and not has_ok:
        return ENTRY_FAILED
    if has_failed and has_ok:
        return ENTRY_PARTIAL
    return ENTRY_OK


def summarise(entries: Iterable[EntryResult]) -> dict[str, int]:
    """Tally entry statuses for the JSON `summary` block."""
    counts = {
        "total": 0,
        ENTRY_OK: 0,
        ENTRY_PARTIAL: 0,
        ENTRY_FAILED: 0,
        ENTRY_SKIPPED: 0,
        ENTRY_WOULD_RUN: 0,
    }
    for e in entries:
        counts["total"] += 1
        counts[e.status] = counts.get(e.status, 0) + 1
    return counts


# ---------- stderr handling ----------


def tail_stderr(stderr: str | None, *, lines: int = STDERR_TAIL_LINES) -> str | None:
    """Return the last `lines` of stderr, or None if stderr is empty.

    Trailing newlines on the whole string are dropped so the tail does
    not start with a blank line; internal blanks are preserved (they
    often separate logical sections in package-manager output).
    """
    if not stderr:
        return None
    rstripped = stderr.rstrip("\n")
    if not rstripped:
        return None
    return "\n".join(rstripped.splitlines()[-lines:])


# ---------- JSON ----------


def format_json_summary(report: RecoveryReport) -> dict:
    """Return the JSON-friendly dict per schema v1.

    Top-level shape:
      schema_version, timestamp, distro_id, dry_run, ordering_note,
      summary {total, ok, partial, failed, skipped, would-run},
      entries [...]

    Per entry:
      id, name, is_service, status, steps [...]

    Per step:
      name, status; plus the fields applicable to that step name
      (argv/exit_code/duration_s/stderr_tail for install+enable;
      snapshot/files_* for restore; reason for skipped/would-run).
    """
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "timestamp": report.timestamp,
        "distro_id": report.distro_id,
        "dry_run": report.dry_run,
        "ordering_note": report.ordering_note,
        "summary": summarise(report.entries),
        "entries": [_entry_to_json(e) for e in report.entries],
    }


def _entry_to_json(entry: EntryResult) -> dict:
    return {
        "id": entry.entry_id,
        "name": entry.entry_name,
        "is_service": entry.is_service,
        "status": entry.status,
        "steps": [_step_to_json(s) for s in entry.steps],
    }


def _step_to_json(step: StepResult) -> dict:
    out: dict = {"name": step.name, "status": step.status}
    # Carry argv whenever known — gives the operator a copy-pasteable
    # command to retry by hand, mirroring restore's undo banner.
    if step.argv is not None:
        out["argv"] = list(step.argv)
    if step.exit_code is not None:
        out["exit_code"] = step.exit_code
    if step.duration_s is not None:
        out["duration_s"] = round(step.duration_s, 3)
    # Restore-specific metrics.
    if step.snapshot is not None:
        out["snapshot"] = step.snapshot
    if step.files_restored is not None:
        out["files_restored"] = step.files_restored
    if step.files_failed is not None:
        out["files_failed"] = step.files_failed
    if step.extras_reported is not None:
        out["extras_reported"] = step.extras_reported
    if step.reason:
        out["reason"] = step.reason
    # The reason for stderr_tail living in JSON, not just the log,
    # is decision 5 — scripts shouldn't have to parse the human log
    # to learn why a step failed.
    if step.stderr_tail:
        out["stderr_tail"] = step.stderr_tail
    return out


def format_json(report: RecoveryReport) -> str:
    """JSON-encoded summary, pretty-printed for human re-reading."""
    return json.dumps(format_json_summary(report), indent=2, sort_keys=False)


# ---------- human log ----------


_STATUS_TAGS = {
    STATUS_OK: "OK",
    STATUS_FAILED: "FAIL",
    STATUS_SKIPPED: "SKIPPED",
    STATUS_WOULD_RUN: "WOULD-RUN",
}


def format_human_log(report: RecoveryReport) -> str:
    """Multi-line human log. The artifact written to ``recovery-TS.log``."""
    lines: list[str] = []
    header_suffix = " [DRY RUN]" if report.dry_run else ""
    lines.append(
        f"── recovery {report.timestamp} ({report.distro_id}){header_suffix} ──"
    )
    # Decision 3 disclosure: ordering is alphabetical, not dependency-aware.
    lines.append(f"note: {report.ordering_note}")
    lines.append("")

    for entry in report.entries:
        lines.append(entry.entry_id)
        for step in entry.steps:
            lines.extend(_format_step_lines(step))
        lines.append("")

    summary = summarise(report.entries)
    lines.append(
        "── summary: "
        f"{summary[ENTRY_OK]} OK · {summary[ENTRY_PARTIAL]} partial · "
        f"{summary[ENTRY_FAILED]} failed · {summary[ENTRY_SKIPPED]} skipped"
        f"{' · ' + str(summary[ENTRY_WOULD_RUN]) + ' would-run' if summary[ENTRY_WOULD_RUN] else ''}"
        " ──"
    )
    return "\n".join(lines) + "\n"


def _format_step_lines(step: StepResult) -> list[str]:
    tag = _STATUS_TAGS.get(step.status, step.status.upper())
    detail = _step_detail(step)
    main = f"  {step.name:<7} : {tag:<9} {detail}".rstrip()
    out = [main]
    if step.stderr_tail:
        # Indented under the step line so the eye finds the cause
        # right below the FAIL marker, not in a sidecar.
        out.append("      stderr (tail):")
        for ln in step.stderr_tail.splitlines():
            out.append(f"        {ln}")
    return out


def _step_detail(step: StepResult) -> str:
    """The parenthesised detail after the status tag, varies by step name."""
    if step.status == STATUS_SKIPPED:
        return f"({step.reason})" if step.reason else ""

    if step.name == "restore":
        if step.status == STATUS_WOULD_RUN:
            return (
                f"({step.files_restored or 0} file(s) from snapshot {step.snapshot})"
                if step.snapshot else ""
            )
        parts = []
        if step.files_restored is not None:
            parts.append(f"{step.files_restored} file(s)")
        if step.files_failed:
            parts.append(f"{step.files_failed} failed")
        if step.extras_reported:
            parts.append(f"{step.extras_reported} extra(s) reported")
        if step.snapshot:
            parts.append(f"snapshot {step.snapshot}")
        return f"({', '.join(parts)})" if parts else ""

    # install / enable: argv + exit + duration
    argv_str = " ".join(step.argv) if step.argv else ""
    if step.status == STATUS_WOULD_RUN:
        return f"({argv_str})" if argv_str else ""
    parts = []
    if argv_str:
        parts.append(argv_str)
    if step.status == STATUS_FAILED and step.exit_code is not None:
        parts.append(f"exit {step.exit_code}")
    if step.duration_s is not None:
        parts.append(f"{step.duration_s:.1f}s")
    return f"({', '.join(parts)})" if parts else ""


# ---------- artifact paths (pure) ----------


def artifact_paths(store_root: Path, timestamp: str) -> tuple[Path, Path]:
    """Return ``(log_path, json_path)`` under ``<store_root>/recovery/``.

    Pure path math; does not create the directory. The orchestrator /
    CLI creates and chowns the recovery dir before calling the writer.
    """
    base = store_root / "recovery"
    return (
        base / f"recovery-{timestamp}.log",
        base / f"recovery-{timestamp}.json",
    )
