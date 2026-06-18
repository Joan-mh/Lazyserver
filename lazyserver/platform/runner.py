"""Subprocess wrapper with dry-run mode (AGENTS.md, NFR-3, arch §6).

Every external command goes through here. argv only — never `shell=True` —
both to honour the schema §5 "no shell" decision and to keep command-injection
risk minimal. Every call is logged.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("lazyserver.runner")

DRY_RUN_EXIT = 0


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    dry_run: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    dry_run: bool = False,
    capture: bool = True,
) -> RunResult:
    """Run argv and return a RunResult.

    With dry_run=True, no process is spawned; a synthetic successful result is
    returned so callers can be exercised in tests without touching the system.

    `capture=False` lets the child inherit stdin/stdout/stderr — needed for
    interactive programs like `$EDITOR` that must talk to the terminal.
    """
    if not argv:
        raise ValueError("run() requires a non-empty argv.")
    argv_tuple = tuple(argv)

    if dry_run:
        log.info("dry-run: would exec %s", argv_tuple)
        return RunResult(
            argv=argv_tuple,
            exit_code=DRY_RUN_EXIT,
            stdout="",
            stderr="",
            duration_s=0.0,
            dry_run=True,
        )

    log.info("exec %s", argv_tuple)
    started = time.monotonic()
    completed = subprocess.run(
        argv_tuple,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        timeout=timeout,
        capture_output=capture,
        text=True if capture else False,
        check=False,
    )
    duration = time.monotonic() - started
    result = RunResult(
        argv=argv_tuple,
        exit_code=completed.returncode,
        stdout=completed.stdout or "" if capture else "",
        stderr=completed.stderr or "" if capture else "",
        duration_s=duration,
        dry_run=False,
    )
    log.info(
        "done %s exit=%d in %.3fs",
        argv_tuple,
        result.exit_code,
        result.duration_s,
    )
    if check and not result.ok:
        raise subprocess.CalledProcessError(
            result.exit_code, argv_tuple, output=result.stdout, stderr=result.stderr
        )
    return result
