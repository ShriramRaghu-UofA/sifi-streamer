"""Create and validate editable SiFi sensor profile files."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from sifi_streamer.sensor_profile import (
    SENSOR_PRESETS,
    load_sensor_profile,
    write_sensor_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and validate complete SiFi sensor profiles."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Write an editable JSON profile")
    create.add_argument("path", type=Path)
    create.add_argument("--preset", choices=tuple(SENSOR_PRESETS), default="all")
    create.add_argument("--force", action="store_true")
    validate = commands.add_parser("validate", help="Validate a JSON profile")
    validate.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            write_sensor_profile(
                args.path, SENSOR_PRESETS[args.preset], overwrite=args.force
            )
            print(f"Wrote sensor profile: {args.path}")
        else:
            load_sensor_profile(args.path)
            print(f"Valid sensor profile: {args.path}")
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
