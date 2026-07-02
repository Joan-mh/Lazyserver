"""Recovery report — pure formatter tests (FR-5.3.2, FR-5.3.3).

The formatters are pure: build a `RecoveryReport` in memory, format
to JSON or human log, assert on the result. Two golden tests fix the
artifact shape Joan signed off on — those goldens are intentionally
verbose because changing them later means changing a downstream
contract (scripts, CI, the v2 Ansible exporter).

The substance lives in:

  * `derive_entry_status` — the cascade roll-up rules.
  * `tail_stderr` — bounded stderr capture for failure entries.
  * `format_json_summary` — schema v1 shape, with stderr_tail on
    failures and `dry_run: true` on plan output.
  * `format_human_log` — the artifact the operator reads on a fresh
    box, with the ordering-not-dependency-aware disclosure inline.
"""

from __future__ import annotations

import json

from lazyserver.recovery.report import (
    ENTRY_FAILED,
    ENTRY_OK,
    ENTRY_PARTIAL,
    ENTRY_SKIPPED,
    ENTRY_WOULD_RUN,
    JSON_SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WOULD_RUN,
    STDERR_TAIL_LINES,
    EntryResult,
    RecoveryReport,
    StepResult,
    artifact_paths,
    derive_entry_status,
    format_human_log,
    format_json,
    format_json_summary,
    summarise,
    tail_stderr,
)
from pathlib import Path


# ---------- derive_entry_status ----------


def test_all_ok_is_ok():
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_OK),
            StepResult(name="restore", status=STATUS_OK),
            StepResult(name="enable", status=STATUS_OK),
        ]
    ) == ENTRY_OK


def test_install_fail_cascades_to_failed():
    """The standard `apt-get` fails → restore + enable cascade-skipped
    case. No step succeeded; entry status must be `failed`, not
    `partial` — `partial` would suggest something usable was done."""
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_FAILED, exit_code=100),
            StepResult(name="restore", status=STATUS_SKIPPED, reason="install failed"),
            StepResult(name="enable", status=STATUS_SKIPPED, reason="install failed"),
        ]
    ) == ENTRY_FAILED


def test_restore_fail_with_install_ok_is_partial():
    """Restore-fail does NOT cascade — enable still runs. If install
    and enable succeeded but restore failed, the service is up on
    stock config; partial is the honest summary."""
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_OK),
            StepResult(name="restore", status=STATUS_FAILED),
            StepResult(name="enable", status=STATUS_OK),
        ]
    ) == ENTRY_PARTIAL


def test_all_skipped_is_skipped():
    """Edge: an entry whose every step is non-applicable (e.g. no
    install command, no snapshot, app entry with no enable). Plan
    produced nothing to run; entry status reflects that."""
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_SKIPPED, reason="no install"),
            StepResult(name="restore", status=STATUS_SKIPPED, reason="no snapshots"),
            StepResult(name="enable", status=STATUS_SKIPPED, reason="entry is an app"),
        ]
    ) == ENTRY_SKIPPED


def test_any_would_run_is_would_run():
    """Dry-run: every applicable step is `would-run`; entry rolls up
    to `would-run` so the summary line reflects 'plan' not 'result'."""
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_WOULD_RUN),
            StepResult(name="restore", status=STATUS_SKIPPED, reason="no snapshots"),
            StepResult(name="enable", status=STATUS_WOULD_RUN),
        ]
    ) == ENTRY_WOULD_RUN


def test_app_with_install_ok_restore_ok_no_enable_is_ok():
    """Apps have no enable step — that's a SKIPPED with reason 'app'.
    Install + restore succeeding is full success for the entry."""
    assert derive_entry_status(
        [
            StepResult(name="install", status=STATUS_OK),
            StepResult(name="restore", status=STATUS_OK),
            StepResult(name="enable", status=STATUS_SKIPPED, reason="entry is an app"),
        ]
    ) == ENTRY_OK


# ---------- summarise ----------


def test_summarise_counts_each_status():
    entries = (
        EntryResult("a", "A", True, ENTRY_OK, steps=()),
        EntryResult("b", "B", True, ENTRY_OK, steps=()),
        EntryResult("c", "C", True, ENTRY_PARTIAL, steps=()),
        EntryResult("d", "D", False, ENTRY_FAILED, steps=()),
    )
    s = summarise(entries)
    assert s["total"] == 4
    assert s[ENTRY_OK] == 2
    assert s[ENTRY_PARTIAL] == 1
    assert s[ENTRY_FAILED] == 1
    assert s[ENTRY_SKIPPED] == 0


# ---------- tail_stderr ----------


def test_tail_stderr_returns_last_n_lines():
    text = "\n".join(f"line {i}" for i in range(20))
    tail = tail_stderr(text, lines=5)
    assert tail == "line 15\nline 16\nline 17\nline 18\nline 19"


def test_tail_stderr_handles_short_input():
    assert tail_stderr("only one line") == "only one line"


def test_tail_stderr_returns_none_for_empty_inputs():
    assert tail_stderr(None) is None
    assert tail_stderr("") is None
    assert tail_stderr("\n\n") is None


def test_tail_stderr_default_is_10_lines():
    text = "\n".join(f"l{i}" for i in range(30))
    tail = tail_stderr(text)
    assert tail is not None
    assert len(tail.splitlines()) == STDERR_TAIL_LINES


# ---------- JSON schema ----------


def _sample_run_report() -> RecoveryReport:
    """A realistic mixed-outcome run: one OK service, one partial, one
    failed-install (cascade), one whole-entry skip (no backups). Used
    by the golden tests. The no-backups app locks the new NO_BACKUPS
    reason string into a rendered artifact — silent drift of that
    message would break the golden."""
    bind = EntryResult(
        entry_id="bind9",
        entry_name="bind9",
        is_service=True,
        status=ENTRY_OK,
        steps=(
            StepResult(
                name="install",
                status=STATUS_OK,
                argv=("apt-get", "install", "-y", "bind9"),
                exit_code=0,
                duration_s=12.34,
            ),
            StepResult(
                name="restore",
                status=STATUS_OK,
                snapshot="20260619-080000",
                files_restored=3,
                files_failed=0,
                extras_reported=1,
            ),
            StepResult(
                name="enable",
                status=STATUS_OK,
                argv=("systemctl", "enable", "--now", "bind9"),
                exit_code=0,
                duration_s=0.42,
            ),
        ),
    )
    neovim = EntryResult(
        entry_id="neovim",
        entry_name="Neovim",
        is_service=False,
        status=ENTRY_SKIPPED,
        steps=(
            StepResult(
                name="install",
                status=STATUS_SKIPPED,
                reason="no backups — entry not part of this system",
            ),
            StepResult(
                name="restore",
                status=STATUS_SKIPPED,
                reason="no backups — entry not part of this system",
            ),
            StepResult(
                name="enable",
                status=STATUS_SKIPPED,
                reason="no backups — entry not part of this system",
            ),
        ),
    )
    nginx = EntryResult(
        entry_id="nginx",
        entry_name="nginx",
        is_service=True,
        status=ENTRY_FAILED,
        steps=(
            StepResult(
                name="install",
                status=STATUS_FAILED,
                argv=("apt-get", "install", "-y", "nginx"),
                exit_code=100,
                duration_s=4.12,
                stderr_tail="E: Unable to locate package nginx\nE: Package not found",
            ),
            StepResult(
                name="restore",
                status=STATUS_SKIPPED,
                reason="install failed",
            ),
            StepResult(
                name="enable",
                status=STATUS_SKIPPED,
                reason="install failed",
            ),
        ),
    )
    postfix = EntryResult(
        entry_id="postfix",
        entry_name="Postfix",
        is_service=True,
        status=ENTRY_PARTIAL,
        steps=(
            StepResult(
                name="install",
                status=STATUS_OK,
                argv=("apt-get", "install", "-y", "postfix"),
                exit_code=0,
                duration_s=8.2,
            ),
            StepResult(
                name="restore",
                status=STATUS_OK,
                snapshot="20260618-091500",
                files_restored=2,
                files_failed=0,
                extras_reported=0,
            ),
            StepResult(
                name="enable",
                status=STATUS_FAILED,
                argv=("systemctl", "enable", "--now", "postfix"),
                exit_code=1,
                duration_s=0.3,
                stderr_tail="Failed to enable unit: Unit postfix.service does not exist.",
            ),
        ),
    )
    return RecoveryReport(
        timestamp="20260620-153045",
        distro_id="ubuntu",
        ordering_note=(
            "Entries are processed in alphabetical order by id. "
            "Ordering is not dependency-aware; if one entry depends on "
            "another being installed first, that ordering is not enforced."
        ),
        dry_run=False,
        entries=(bind, neovim, nginx, postfix),
    )


def test_json_schema_top_level_shape():
    """Lock the top-level keys + schema_version. Adding a field is a
    breaking change for downstream parsers; this test forces an
    intentional decision (and a schema_version bump) when it happens."""
    out = format_json_summary(_sample_run_report())
    assert out["schema_version"] == JSON_SCHEMA_VERSION
    assert out["timestamp"] == "20260620-153045"
    assert out["distro_id"] == "ubuntu"
    assert out["dry_run"] is False
    assert "alphabetical" in out["ordering_note"]
    assert "summary" in out and "entries" in out


def test_json_summary_block_counts_per_status():
    out = format_json_summary(_sample_run_report())
    summary = out["summary"]
    assert summary["total"] == 4
    assert summary[ENTRY_OK] == 1  # bind9
    assert summary[ENTRY_PARTIAL] == 1  # postfix
    assert summary[ENTRY_FAILED] == 1  # nginx
    assert summary[ENTRY_SKIPPED] == 1  # neovim (no backups)


def test_json_success_entry_carries_argv_and_duration():
    out = format_json_summary(_sample_run_report())
    bind = next(e for e in out["entries"] if e["id"] == "bind9")
    install = bind["steps"][0]
    assert install["name"] == "install"
    assert install["status"] == STATUS_OK
    assert install["argv"] == ["apt-get", "install", "-y", "bind9"]
    assert install["exit_code"] == 0
    assert install["duration_s"] == 12.34
    # success carries no stderr tail.
    assert "stderr_tail" not in install


def test_json_failure_entry_includes_stderr_tail():
    """Joan's decision 5: scripts reading the JSON shouldn't have to
    also parse the human log to learn *why* a step failed."""
    out = format_json_summary(_sample_run_report())
    nginx = next(e for e in out["entries"] if e["id"] == "nginx")
    install = nginx["steps"][0]
    assert install["status"] == STATUS_FAILED
    assert install["exit_code"] == 100
    assert "stderr_tail" in install
    assert "Unable to locate package nginx" in install["stderr_tail"]


def test_json_restore_step_carries_snapshot_and_counts():
    out = format_json_summary(_sample_run_report())
    bind = next(e for e in out["entries"] if e["id"] == "bind9")
    restore = bind["steps"][1]
    assert restore["snapshot"] == "20260619-080000"
    assert restore["files_restored"] == 3
    assert restore["extras_reported"] == 1
    # No argv on the restore step — restore is internal, not a subprocess.
    assert "argv" not in restore


def test_json_skipped_step_carries_reason():
    """Whole-entry skip case: neovim had zero backups, all three steps
    carry the same NO_BACKUPS reason so a JSON consumer can distinguish
    'we deliberately did nothing' from a cascade or a partial run."""
    out = format_json_summary(_sample_run_report())
    neovim = next(e for e in out["entries"] if e["id"] == "neovim")
    for step in neovim["steps"]:
        assert step["status"] == STATUS_SKIPPED
        assert step["reason"] == "no backups — entry not part of this system"


def test_format_json_round_trips_through_json_module():
    """Sanity: the dict serialises to valid JSON and parses back to
    the same shape — guards against tuples or paths leaking in."""
    text = format_json(_sample_run_report())
    parsed = json.loads(text)
    assert parsed["schema_version"] == JSON_SCHEMA_VERSION
    assert parsed["entries"][0]["id"] == "bind9"


def test_json_dry_run_flag_is_true_for_dry_run():
    """Joan's decision 7: dry-run writes the JSON too, with `dry_run:
    true` at the top level so scripts can distinguish a plan from a
    result without checking step statuses."""
    report = RecoveryReport(
        timestamp="20260620-153045",
        distro_id="ubuntu",
        ordering_note="…",
        dry_run=True,
        entries=(
            EntryResult(
                entry_id="bind9",
                entry_name="bind9",
                is_service=True,
                status=ENTRY_WOULD_RUN,
                steps=(
                    StepResult(
                        name="install",
                        status=STATUS_WOULD_RUN,
                        argv=("apt-get", "install", "-y", "bind9"),
                    ),
                ),
            ),
        ),
    )
    out = format_json_summary(report)
    assert out["dry_run"] is True
    assert out["entries"][0]["status"] == ENTRY_WOULD_RUN
    assert out["entries"][0]["steps"][0]["status"] == STATUS_WOULD_RUN


# ---------- human log goldens ----------


def test_human_log_golden_mixed_outcomes():
    """The human-log shape downstream tooling and the user will read.
    Locking the golden makes any formatting change a deliberate
    decision (the artifact is part of the UX of recovery)."""
    log = format_human_log(_sample_run_report())
    expected = (
        "── recovery 20260620-153045 (ubuntu) ──\n"
        "note: Entries are processed in alphabetical order by id. "
        "Ordering is not dependency-aware; if one entry depends on "
        "another being installed first, that ordering is not enforced.\n"
        "\n"
        "bind9\n"
        "  install : OK        (apt-get install -y bind9, 12.3s)\n"
        "  restore : OK        (3 file(s), 1 extra(s) reported, snapshot 20260619-080000)\n"
        "  enable  : OK        (systemctl enable --now bind9, 0.4s)\n"
        "\n"
        "neovim\n"
        "  install : SKIPPED   (no backups — entry not part of this system)\n"
        "  restore : SKIPPED   (no backups — entry not part of this system)\n"
        "  enable  : SKIPPED   (no backups — entry not part of this system)\n"
        "\n"
        "nginx\n"
        "  install : FAIL      (apt-get install -y nginx, exit 100, 4.1s)\n"
        "      stderr (tail):\n"
        "        E: Unable to locate package nginx\n"
        "        E: Package not found\n"
        "  restore : SKIPPED   (install failed)\n"
        "  enable  : SKIPPED   (install failed)\n"
        "\n"
        "postfix\n"
        "  install : OK        (apt-get install -y postfix, 8.2s)\n"
        "  restore : OK        (2 file(s), snapshot 20260618-091500)\n"
        "  enable  : FAIL      (systemctl enable --now postfix, exit 1, 0.3s)\n"
        "      stderr (tail):\n"
        "        Failed to enable unit: Unit postfix.service does not exist.\n"
        "\n"
        "── summary: 1 OK · 1 partial · 1 failed · 1 skipped ──\n"
    )
    assert log == expected


def test_human_log_dry_run_marks_header_and_status():
    """Dry-run output is visually distinct so the user does not
    confuse a plan for a result."""
    report = RecoveryReport(
        timestamp="20260620-153045",
        distro_id="ubuntu",
        ordering_note="Entries are processed in alphabetical order by id.",
        dry_run=True,
        entries=(
            EntryResult(
                entry_id="bind9",
                entry_name="bind9",
                is_service=True,
                status=ENTRY_WOULD_RUN,
                steps=(
                    StepResult(
                        name="install",
                        status=STATUS_WOULD_RUN,
                        argv=("apt-get", "install", "-y", "bind9"),
                    ),
                    StepResult(
                        name="restore",
                        status=STATUS_WOULD_RUN,
                        snapshot="20260619-080000",
                        files_restored=3,
                    ),
                    StepResult(
                        name="enable",
                        status=STATUS_WOULD_RUN,
                        argv=("systemctl", "enable", "--now", "bind9"),
                    ),
                ),
            ),
        ),
    )
    log = format_human_log(report)
    assert "[DRY RUN]" in log
    assert "WOULD-RUN" in log
    # The summary line surfaces would-run count when present.
    assert "would-run" in log


# ---------- artifact_paths ----------


def test_artifact_paths_uses_store_recovery_subdir():
    log_path, json_path = artifact_paths(Path("/srv/store"), "20260620-153045")
    assert log_path == Path("/srv/store/recovery/recovery-20260620-153045.log")
    assert json_path == Path("/srv/store/recovery/recovery-20260620-153045.json")


def test_artifact_paths_does_not_create_directory(tmp_path):
    """Pure path math; the orchestrator/CLI handles dir creation +
    chown so a root-run session leaves the dir owned by target_user."""
    artifact_paths(tmp_path, "20260620-153045")
    assert not (tmp_path / "recovery").exists()
