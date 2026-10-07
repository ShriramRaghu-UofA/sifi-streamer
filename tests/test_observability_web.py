import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from sifi_streamer.acquisition import (
    HealthThresholds,
    SignalChannelSpec,
    SignalStreamSpec,
    create_capture_runtime,
)
from sifi_streamer.capture import CaptureLogReader, SegmentStarted
from sifi_streamer.exceptions import CaptureInitializationError, DeviceError
from sifi_streamer.sifi import create_sifi_capture_runtime
from sifi_streamer.web import (
    AnnotationKindDefinition,
    AnnotationKindRegistry,
    AnnotationTarget,
    WebCaptureCoordinator,
)
from sifi_streamer.web.coordinator import _Handler, _WebServer


class CustomPacket:
    def __init__(self, index: int) -> None:
        self.stream_id = "force/left"
        self.timestamps = [index / 100]
        self.data = {"force": [index], "quality": [None if index % 2 else index]}
        self.reported_rate_hz = 99.5
        self.samples_lost = 0
        self.status = "ok"
        self._index = index

    def capture_document(self) -> dict[str, object]:
        return {
            "packet_type": self.stream_id,
            "timestamps": self.timestamps,
            "data": self.data,
            "index": self._index,
        }


class CustomDevice:
    def __init__(self) -> None:
        self._connected = False
        self._index = 0

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        return (
            SignalStreamSpec(
                "force/left",
                (
                    SignalChannelSpec("force", "Force", "N"),
                    SignalChannelSpec("quality", "Quality"),
                ),
                100,
                np.int64,
                "Left force",
            ),
        )

    @property
    def device_info(self) -> dict[str, object]:
        return {"name": "custom"}

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def read_packet(self) -> CustomPacket:
        if not self._connected:
            raise RuntimeError("not connected")
        time.sleep(0.001)
        self._index += 1
        return CustomPacket(self._index)


class ObservabilityTests(unittest.TestCase):
    def test_startup_failure_returns_json_and_preserves_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Mock()
            runtime.monitor.device_info = {}
            runtime.monitor.launch_configuration = {}

            def fail():
                try:
                    raise DeviceError("physical sensor report missing")
                except DeviceError as exc:
                    raise CaptureInitializationError("backend failed") from exc

            runtime.controller.start.side_effect = fail
            coordinator = WebCaptureCoordinator(
                Path(directory) / "failed.capture.jsonl.zst",
                lambda capture_id, attributes, configuration: runtime,
            )
            server = _WebServer(("127.0.0.1", 0), _Handler)
            server.coordinator = coordinator
            server.token = "test"
            server.origin = f"http://127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for _ in range(2):
                    request = Request(
                        f"{server.origin}/api/capture/start",
                        data=json.dumps({"capture_id": "failed"}).encode(),
                        headers={
                            "X-SiFi-Session-Token": "test",
                            "Content-Type": "application/json",
                        },
                    )
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=5)
                    with error.exception as response:
                        self.assertEqual(response.code, 400)
                        result = json.load(response)
                    self.assertEqual(result["state"], "failed")
                    self.assertIn("physical sensor report missing", result["error"])
                    self.assertNotIn("already started", result["error"])
                self.assertEqual(coordinator.bootstrap()["error"], result["error"])
                self.assertEqual(coordinator.live({})["error"], result["error"])
                runtime.controller.start.assert_called_once()
                runtime.controller.close.assert_called_once_with("startup_failure")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_factory_failure_sets_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator = WebCaptureCoordinator(
                Path(directory) / "failed.capture.jsonl.zst",
                Mock(side_effect=DeviceError("factory failed")),
            )
            with self.assertRaisesRegex(DeviceError, "factory failed"):
                coordinator.start("failed", {})
            self.assertEqual(coordinator.bootstrap()["state"], "failed")
            self.assertEqual(coordinator.bootstrap()["error"], "factory failed")

    def test_custom_stream_validity_cursor_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "custom.capture.jsonl.zst"
            runtime = create_capture_runtime(output, "custom", CustomDevice)
            runtime.controller.start()
            try:
                time.sleep(0.04)
                streams = runtime.monitor.streams
                self.assertEqual(streams[0].stream_id, "force/left")
                self.assertEqual(streams[0].channel_units, ("N", None))
                window = runtime.monitor.read_since("force/left", 0)
                assert window is not None
                self.assertEqual(window.samples.dtype, np.dtype(np.int64))
                self.assertTrue(window.validity[:, 0].all())
                self.assertTrue((~window.validity[:, 1]).any())
                self.assertIsNone(
                    runtime.monitor.read_since("force/left", window.end_index)
                )
                time.sleep(0.3)
                health = runtime.monitor.latest()
                assert health is not None
                self.assertGreater(health.streams[0].sample_count, 0)
                self.assertGreater(health.streams[0].missing_fraction, 0)
            finally:
                runtime.controller.close()
            self.assertGreater(len(tuple(CaptureLogReader(output))), 2)

    def test_annotation_kind_generation_and_defaults(self) -> None:
        registry = AnnotationKindRegistry(
            [
                AnnotationKindDefinition(
                    AnnotationTarget.SEGMENT,
                    "Task",
                    id_prefix="Task",
                    default_attributes={"phase": "work"},
                )
            ]
        )
        registry.reserve(AnnotationTarget.SEGMENT, "Task_01")
        self.assertEqual(registry.generate(AnnotationTarget.SEGMENT, "Task"), "Task_02")
        self.assertEqual(
            registry.merge_attributes(
                AnnotationTarget.SEGMENT, "Task", {"phase": "rest", "block": 1}
            ),
            {"phase": "rest", "block": 1},
        )

    def test_web_coordinator_lifecycle_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "web.capture.jsonl.zst"

            def factory(capture_id, attributes, configuration):
                return create_sifi_capture_runtime(
                    output,
                    capture_id,
                    attributes,
                    synthetic=True,
                    thresholds=HealthThresholds(stale_after_seconds=10),
                )

            coordinator = WebCaptureCoordinator(
                output,
                factory,
                definitions=(
                    AnnotationKindDefinition(AnnotationTarget.SEGMENT, "Task"),
                    AnnotationKindDefinition(AnnotationTarget.MARKER, "Note"),
                ),
            )
            coordinator.start("web", {"operator": "test"})
            with self.assertLogs("sifi_streamer.web", level="INFO") as messages:
                segment = coordinator.start_segment("Task", {})
                self.assertEqual(segment, "Task_01")
                coordinator.marker("Note", {"value": 1})
                coordinator.stop_segment(segment)
            output_messages = "\n".join(messages.output)
            self.assertIn("Started segment 'Task_01'", output_messages)
            self.assertIn("Added marker 'Note_01'", output_messages)
            self.assertIn("Stopped segment 'Task_01'", output_messages)
            time.sleep(0.02)
            live = coordinator.live({})
            batches = live["batches"]
            assert isinstance(batches, dict)
            self.assertIn("emg_armband", batches)
            coordinator.stop()
            self.assertEqual(coordinator.bootstrap()["state"], "stopped")
            self.assertTrue(Path(directory, "web.health.jsonl").exists())
            self.assertTrue(
                any(
                    isinstance(record, SegmentStarted)
                    and record.segment_id == "Task_01"
                    for record in CaptureLogReader(output)
                )
            )


if __name__ == "__main__":
    unittest.main()
