"""argparse entry point for `lazyserver` / `lsrv`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyserver",
        description="Configure, back up, restore, and learn about Linux services.",
    )
    parser.add_argument("--version", action="version", version=f"lazyserver {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and display commands without executing them.",
    )
    sub = parser.add_subparsers(dest="command")

    backup = sub.add_parser(
        "backup",
        help="Back up managed files (FR-2.3/2.4). Reused by recovery.",
    )
    mode = backup.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="Show pending files without writing.",
    )
    mode.add_argument(
        "--all",
        dest="all_pending",
        action="store_true",
        help="Back up every pending file across every entry.",
    )
    mode.add_argument(
        "--entry",
        nargs="+",
        metavar="ID",
        help="Back up these entries' pending files only.",
    )
    backup.add_argument(
        "--store",
        type=Path,
        metavar="PATH",
        help="Override backup store path (else settings.backup_store).",
    )

    restore = sub.add_parser(
        "restore",
        help="Restore files from snapshots (FR-3, Phase 5).",
    )
    rmode = restore.add_mutually_exclusive_group(required=True)
    rmode.add_argument(
        "--file",
        type=Path,
        metavar="PATH",
        help="Restore one live file from a snapshot.",
    )
    rmode.add_argument(
        "--entry",
        metavar="ID",
        help="Restore every file of one entry from a snapshot.",
    )
    rmode.add_argument(
        "--all",
        dest="all_entries",
        action="store_true",
        help="Restore every entry's latest snapshot.",
    )
    restore.add_argument(
        "--snapshot",
        metavar="TS",
        help=(
            "Pin a specific snapshot timestamp (not valid with --all). "
            "Default: latest available for each entry."
        ),
    )
    restore.add_argument(
        "--store",
        type=Path,
        metavar="PATH",
        help="Override backup store path (else settings.backup_store).",
    )

    recover = sub.add_parser(
        "recover",
        help=(
            "Full recovery (FR-5.3): install every entry, restore its "
            "latest deliberate backup, enable services. Mirrors the "
            "Phase 5 restore exit codes (0 ok / 1 hard error / 2 partial)."
        ),
    )
    recover.add_argument(
        "--all",
        action="store_true",
        help="Recover every entry. Required (per-entry recovery is `lsrv restore --entry ID`).",
    )
    recover.add_argument(
        "--store",
        type=Path,
        metavar="PATH",
        help="Override backup store path (else settings.backup_store).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        from .app import BootstrapError, run

        try:
            return run(dry_run=args.dry_run)
        except BootstrapError as exc:
            print(f"lazyserver: {exc}", file=sys.stderr)
            return 1

    if args.command == "backup":
        from .backup.cli import cmd_backup

        return cmd_backup(
            list_only=args.list_only,
            all_pending=args.all_pending,
            entry_ids=args.entry,
            store_override=args.store,
            dry_run=args.dry_run,
        )

    if args.command == "restore":
        from .backup.restore_cli import cmd_restore

        return cmd_restore(
            file=args.file,
            entry=args.entry,
            all_entries=args.all_entries,
            snapshot=args.snapshot,
            store_override=args.store,
            dry_run=args.dry_run,
        )

    if args.command == "recover":
        from .recovery.cli import cmd_recover

        return cmd_recover(
            all_entries=args.all,
            store_override=args.store,
            dry_run=args.dry_run,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
