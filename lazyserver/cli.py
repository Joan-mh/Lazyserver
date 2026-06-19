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

    recover = sub.add_parser(
        "recover",
        help="Full recovery (Phase 6) — not yet implemented.",
    )
    recover.add_argument("--all", action="store_true")

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

    if args.command == "recover":
        print("recover: not yet implemented (Phase 6).", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
