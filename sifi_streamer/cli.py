"""Standalone SiFi capture command."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from sifi_streamer.bridge import EMG_SAMPLE_RATES, BridgeTransport
from sifi_streamer.runner import (
    parse_attributes,
    run_interactive_capture,
    run_timed_capture,
    run_until_interrupt,
)
from sifi_streamer.sifi_backend import create_sifi_capture


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone ``sifi-capture`` command-line parser."""
    parser = argparse.ArgumentParser(
        description="Write an authoritative SiFi capture log."
    )
    parser.add_argument("output", type=Path, help="New .capture.jsonl.zst output path")
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--attribute", action="append", default=[], metavar="KEY=VALUE")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--duration", type=float, help="Capture duration in seconds")
    mode.add_argument(
        "--interactive", action="store_true", help="Accept marker and segment commands"
    )
    parser.add_argument(
        "--bridge-executable", type=Path, default=Path("bin/sifibridge.exe")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--transport", choices=tuple(BridgeTransport), default=BridgeTransport.TCP
    )
    parser.add_argument(
        "--emg-sample-rate", type=int, choices=sorted(EMG_SAMPLE_RATES), default=1600
    )
    parser.add_argument(
        "--synthetic", action="store_true", help="Use generated EMG instead of hardware"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone capture command and return zero on success.

    The command refuses an existing output path, validates mode arguments before
    starting hardware, and owns controller startup and shutdown through a runner.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    controller = create_sifi_capture(
        args.output,
        args.capture_id,
        parse_attributes(args.attribute),
        bridge_executable=args.bridge_executable,
        host=args.host,
        port=args.port,
        transport=args.transport,
        emg_sample_rate=args.emg_sample_rate,
        synthetic=args.synthetic,
    )
    if args.interactive:
        reason = run_interactive_capture(controller)
    elif args.duration is not None:
        reason = run_timed_capture(controller, args.duration)
    else:
        print("Recording. Press Ctrl+C to stop.")
        reason = run_until_interrupt(controller)
    print(f"Capture saved to {args.output} ({reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
