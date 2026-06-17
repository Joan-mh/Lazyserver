"""argparse entry point for `lazyserver` / `lsrv`."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyserver",
        description="Configure, back up, restore, and learn about Linux services.",
    )
    parser.add_argument("--version", action="version", version=f"lazyserver {__version__}")
    sub = parser.add_subparsers(dest="command")

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
        print("LazyServer TUI is not implemented yet (Phase 2+).", file=sys.stderr)
        print("Try `lazyserver --version`.", file=sys.stderr)
        return 0

    if args.command == "recover":
        print("recover: not yet implemented (Phase 6).", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
