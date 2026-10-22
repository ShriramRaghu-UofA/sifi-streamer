"""SiFi launcher selection and named-device composition without hardware."""

import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

from sifi_streamer.capture import CaptureLogReader
from sifi_streamer.sifi.bridge import BridgeTransport
from sifi_streamer.sifi.cli import capture
from sifi_streamer.sifi.cli.device_options import resolve_devices
from sifi_streamer.sifi.composition import (
    _device_factories,
    create_sifi_capture_runtime,
)
from sifi_streamer.sifi.devices import SyntheticSiFiDevice
from sifi_streamer.web import cli as web


class InvalidSelection(TypedDict, total=False):
    device_handles: dict[str, str]
    synthetic: bool
    device_handle: str
    transport: str
    port: int


class MultiSiFiTests(unittest.TestCase):
    def test_named_devices_reach_both_launchers(self) -> None:
        for launcher in (capture, web):
            with (
                self.subTest(launcher=launcher.__name__),
                tempfile.TemporaryDirectory() as directory,
                patch.object(capture, "create_sifi_capture") as create,
                patch.object(
                    capture, "run_timed_capture", return_value="normal_completion"
                ),
                patch.object(web, "create_sifi_capture_runtime") as runtime,
                patch.object(web, "serve_capture_web") as serve,
            ):
                args = [
                    str(Path(directory) / "test.capture.duckdb"),
                    "--capture-id",
                    "test",
                    "--device",
                    "left=BioPoint_A",
                    "--device",
                    "right=BioPoint_B",
                ]
                if launcher is capture:
                    args += ["--duration", "1"]
                self.assertEqual(launcher.main(args), 0)
                if launcher is web:
                    serve.call_args.args[1]("test", {}, {})
                    create = runtime
                self.assertEqual(
                    create.call_args.kwargs["device_handles"],
                    {"left": "BioPoint_A", "right": "BioPoint_B"},
                )

    def test_invalid_selections_rejected_before_hardware(self) -> None:
        for values in (
            ["left=A", "left=B"],
            ["left=A", "right=A"],
            ["missing"],
            ["=A"],
            ["left="],
            ["a/b=A"],
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_devices(values)
        for launcher in (capture, web):
            for selection in (
                ["--device", "left=A", "--synthetic"],
                ["--device", "left=A", "--device-handle", "B"],
            ):
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    launcher.main(
                        ["unused.capture.duckdb", "--capture-id", "x", *selection]
                    )

    def test_bridge_factories_have_independent_handles_and_ports(self) -> None:
        for transport in BridgeTransport:
            factories, configuration = _device_factories(
                device_handles={"left": "A", "right": "B"},
                bridge_executable="bridge",
                device_handle=None,
                host="127.0.0.1",
                port=5000,
                transport=transport,
                sensor_profile=None,
                synthetic=False,
            )
            assert isinstance(factories, dict)
            for index, name in enumerate(("left", "right")):
                factory = factories[name]
                assert isinstance(factory, partial)
                self.assertEqual(factory.keywords["device_handle"], ("A", "B")[index])
                self.assertEqual(
                    factory.keywords["port"],
                    5000 if transport == BridgeTransport.STDOUT else 5000 + index,
                )
            self.assertIn("devices", configuration)

    def test_one_and_two_device_composition_records_samples(self) -> None:
        for handles in ({"left": "A"}, {"left": "A", "right": "B"}):
            with (
                self.subTest(handles=handles),
                tempfile.TemporaryDirectory() as directory,
                patch(
                    "sifi_streamer.sifi.composition._device_factory",
                    return_value=SyntheticSiFiDevice,
                ),
            ):
                path = Path(directory) / "test.capture.duckdb"
                runtime = create_sifi_capture_runtime(
                    path, "test", device_handles=handles
                )
                runtime.controller.start()
                time.sleep(0.15)
                runtime.controller.close()
                reader = CaptureLogReader(path)
                self.assertEqual(
                    reader.query("SELECT outcome FROM capture_status"), [("completed",)]
                )
                rows = reader.query(
                    "SELECT device_id, count(*) FROM packets GROUP BY device_id"
                )
                counts: dict[str, int] = {}
                for name, count in rows:
                    assert isinstance(name, str) and isinstance(count, int)
                    counts[name] = count
                self.assertEqual(set(counts), set(handles))
                self.assertTrue(all(count > 0 for count in counts.values()))

    def test_invalid_composition_rejected(self) -> None:
        invalid: tuple[InvalidSelection, ...] = (
            {"device_handles": {}},
            {"device_handles": {"left": "A", "right": "A"}},
            {"device_handles": {"left": "A"}, "synthetic": True},
            {"device_handles": {"left": "A"}, "device_handle": "B"},
            {
                "device_handles": {"left": "A", "right": "B"},
                "transport": "tcp",
                "port": 65535,
            },
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                create_sifi_capture_runtime(
                    Path("unused.capture.duckdb"), "x", **options
                )
