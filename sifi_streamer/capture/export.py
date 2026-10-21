"""Export selected capture database tables to ordinary Parquet or CSV."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from sifi_streamer.capture.records import CaptureError, CaptureLogReader


def build_parser() -> argparse.ArgumentParser:
    """Build the device-neutral export command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing .capture.duckdb database")
    parser.add_argument("output", type=Path, help="New export directory")
    parser.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    parser.add_argument(
        "--table",
        action="append",
        dest="tables",
        help="Export only this table; repeat to select multiple tables",
    )
    parser.add_argument(
        "--native-variant",
        action="store_true",
        help="Use native Parquet VARIANT instead of JSON text",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Export without importing a device integration or optional table library."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.native_variant and args.format != "parquet":
        parser.error("--native-variant requires --format parquet")
    try:
        CaptureLogReader(args.source).export(
            args.output,
            format=args.format,
            native_variant=args.native_variant,
            tables=args.tables,
        )
    except (CaptureError, OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Capture tables exported to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
