"""Command-line launcher for the local capture dashboard."""

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from sifi_streamer.annotation_kinds import AnnotationKindDefinition
from sifi_streamer.bridge import BridgeTransport
from sifi_streamer.health import HealthThresholds
from sifi_streamer.runner import parse_attributes
from sifi_streamer.sensor_cli import (
    add_sensor_arguments,
    resolve_sensor_profile,
    sensor_options_used,
    sensor_profile_summary,
)
from sifi_streamer.sifi_backend import create_sifi_capture_runtime
from sifi_streamer.web import _kind, serve_capture_web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve a local monitored SiFi capture dashboard."
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--capture-id", default="capture")
    parser.add_argument("--attribute", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--kinds-file", type=Path)
    parser.add_argument(
        "--bridge-executable", type=Path, default=Path("bin/sifibridge.exe")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--transport", choices=tuple(BridgeTransport), default=BridgeTransport.TCP
    )
    add_sensor_arguments(parser)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--health-window", type=float, default=5.0)
    parser.add_argument("--stale-after", type=float, default=2.0)
    parser.add_argument("--minimum-rate-ratio", type=float, default=0.9)
    parser.add_argument("--maximum-rate-ratio", type=float, default=1.1)
    parser.add_argument("--maximum-missing-fraction", type=float, default=0.0)
    parser.add_argument("--maximum-lost-samples", type=int, default=0)
    parser.add_argument("--no-health-log", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--web-port", type=int, default=0)
    return parser


def _definitions(path: Path | None) -> tuple[AnnotationKindDefinition, ...]:
    if path is None:
        return ()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("kinds file must contain a JSON array of objects")
    return tuple(_kind(item) for item in value)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.synthetic and sensor_options_used(args):
        parser.error("sensor profile options cannot be used with --synthetic")
    try:
        thresholds = HealthThresholds(
            args.health_window,
            args.stale_after,
            args.minimum_rate_ratio,
            args.maximum_rate_ratio,
            args.maximum_missing_fraction,
            args.maximum_lost_samples,
        )
        definitions = _definitions(args.kinds_file)
        attributes = parse_attributes(args.attribute)
        sensor_profile = None if args.synthetic else resolve_sensor_profile(args)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    def factory(capture_id: str, capture_attributes):
        return create_sifi_capture_runtime(
            args.output,
            capture_id,
            capture_attributes,
            bridge_executable=args.bridge_executable,
            host=args.host,
            port=args.port,
            transport=args.transport,
            sensor_profile=sensor_profile,
            synthetic=args.synthetic,
            thresholds=thresholds,
        )

    configuration_summary = {
        "device": "synthetic" if args.synthetic else "SiFi bridge",
        "bridge_executable": str(args.bridge_executable),
        "host": args.host,
        "port": args.port,
        "transport": str(args.transport),
    }
    if sensor_profile is not None:
        configuration_summary.update(sensor_profile_summary(sensor_profile))
    serve_capture_web(
        args.output,
        factory,
        configuration_summary=configuration_summary,
        default_capture_id=args.capture_id,
        default_attributes=attributes,
        thresholds=thresholds,
        definitions=definitions,
        health_log_enabled=not args.no_health_log,
        port=args.web_port,
        open_browser=not args.no_open,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
