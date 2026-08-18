"""Command-line export of one SiFi capture to canonical Parquet tables."""

import argparse
from collections.abc import Sequence
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing .capture.jsonl.zst file")
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination dataset directory (defaults to <capture-base>.parquet)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing complete export"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.source.is_file():
        parser.error(f"capture file not found: {args.source}")
    try:
        from sifi_streamer.sifi.export import export_sifi_capture_to_parquet
    except ModuleNotFoundError as exc:
        if exc.name in {"pandas", "pyarrow"}:
            parser.error(
                "Parquet export requires the optional dependencies; install "
                "sifi-streamer[parquet]"
            )
        raise
    try:
        output = export_sifi_capture_to_parquet(
            args.source, args.output, force=args.force
        )
    except ImportError:
        parser.error(
            "Parquet export requires the optional dependencies; install "
            "sifi-streamer[parquet]"
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"SiFi tables exported to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
