"""Rewrite ``api/openapi.json``, the OpenAPI contract snapshot of pylovo-api.

Usage (from the repository root)::

    uv run --extra api python api/scripts/export_openapi.py            # rewrite the snapshot
    uv run --extra api python api/scripts/export_openapi.py --check    # exit 1 if it is outdated

Needs no database: the schema comes from the app's routes alone.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pylovo_api.openapi import SNAPSHOT, UPDATE_HINT, render


def main(argv: list[str] | None = None) -> int:
    """Write (or with ``--check`` compare) the snapshot; returns the exit code."""
    parser = argparse.ArgumentParser(description="Rewrite api/openapi.json from the pylovo-api app.")
    parser.add_argument("--check", action="store_true", help="only compare, exit 1 if the snapshot is outdated")
    parser.add_argument("--output", type=Path, default=SNAPSHOT, help=f"snapshot file (default: {SNAPSHOT})")
    args = parser.parse_args(argv)
    text = render()
    current = args.output.read_text(encoding="utf-8") if args.output.is_file() else None
    if args.check:
        if current != text:
            print(f"{args.output} is outdated. {UPDATE_HINT}", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0
    if current == text:
        print(f"{args.output} is up to date")
    else:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
