import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from sifi_streamer.annotation_kinds import (
    AnnotationKindDefinition,
    AnnotationKindRegistry,
    AnnotationTarget,
)
from sifi_streamer.capture import CaptureLogReader, SegmentStarted
from sifi_streamer.devices import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.health import HealthThresholds
from sifi_streamer.sifi_backend import (
    create_capture_runtime,
    create_sifi_capture_runtime,
)
from sifi_streamer.web import WebCaptureCoordinator


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

            def factory(capture_id, attributes):
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
            segment = coordinator.start_segment("Task", {})
            self.assertEqual(segment, "Task_01")
            coordinator.marker("Note", {"value": 1})
            coordinator.stop_segment(segment)
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
