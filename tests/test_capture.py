import json
import tempfile
import unittest
from compression import zstd
from pathlib import Path

from sifi_streamer.background.recorder import RecorderFSM
from sifi_streamer.capture import (
    CaptureDecodeError,
    CaptureLifecycleError,
    CaptureLogReader,
    CaptureLogWriter,
    CaptureStarted,
    CaptureStopped,
    Marker,
    RawPacket,
    SegmentStarted,
    SegmentStopped,
    decode_record,
    encode_record,
    validate_attributes,
)
from sifi_streamer.config import StreamerConfig


class CaptureTests(unittest.TestCase):
    def test_recorder_preserves_startup_device_info_as_first_raw_document(self) -> None:
        device_info: dict[str, object] = {
            "info": {
                "id": "SB_3F4B",
                "connected": True,
                "device": {"firmware_version": "5.0", "emg": {"fs": 2000.0}},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.capture.jsonl.zst"
            recorder = RecorderFSM(StreamerConfig(), device_info)
            recorder.start_capture(path, "capture")
            recorder.stop_capture()

            records = list(CaptureLogReader(path))

        self.assertIsInstance(records[0], CaptureStarted)
        self.assertIsInstance(records[1], RawPacket)
        startup_info = records[1]
        assert isinstance(startup_info, RawPacket)
        self.assertEqual(startup_info.packet, device_info)
        self.assertIsInstance(records[2], CaptureStopped)

    def test_each_wire_record_decodes_to_its_concrete_type(self) -> None:
        common = {
            "schema_version": 2,
            "sequence": 0,
            "host_monotonic_ns": 1,
            "host_unix_ns": 2,
        }
        cases = (
            ({**common, "record_type": "raw_packet", "packet": {}}, RawPacket),
            (
                {
                    **common,
                    "record_type": "capture_started",
                    "capture_id": "capture",
                    "attributes": {},
                },
                CaptureStarted,
            ),
            (
                {
                    **common,
                    "record_type": "capture_stopped",
                    "reason": "completed",
                },
                CaptureStopped,
            ),
            (
                {
                    **common,
                    "record_type": "segment_started",
                    "segment_id": "segment",
                    "segment_kind": "kind",
                    "attributes": {},
                },
                SegmentStarted,
            ),
            (
                {
                    **common,
                    "record_type": "segment_stopped",
                    "segment_id": "segment",
                    "reason": None,
                },
                SegmentStopped,
            ),
            (
                {
                    **common,
                    "record_type": "marker",
                    "marker_id": "marker",
                    "marker_kind": "kind",
                    "attributes": {},
                    "source_time_ns": None,
                    "source_clock": None,
                },
                Marker,
            ),
        )
        for wire, expected_type in cases:
            with self.subTest(record_type=wire["record_type"]):
                self.assertIs(type(decode_record(wire)), expected_type)

    def test_round_trip_and_marker_order(self) -> None:
        marker = Marker(2, 4, 5, 6, "occurrence-1", "button", {"ok": True}, None, None)
        self.assertEqual(decode_record(json.loads(encode_record(marker))), marker)
        wire = json.loads(encode_record(marker))
        self.assertEqual(wire["marker_id"], "occurrence-1")
        self.assertEqual(wire["marker_kind"], "button")

    def test_capture_round_trip_preserves_complete_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.capture.jsonl.zst"
            document = {"packet_type": "emg_armband", "vendor": {"future": 3}}
            with CaptureLogWriter(path, "capture", {"operator": "one"}) as writer:
                writer.append_packet(document)
                writer.start_segment("rest-1", "rest", {"level": 2})
                writer.append_marker("note-1", "note", {"ready": True})
                writer.stop_segment("rest-1", "completed")
            records = list(CaptureLogReader(path))
            raw = next(record for record in records if isinstance(record, RawPacket))
            self.assertEqual(raw.packet, document)

    def test_reader_decodes_source_compatible_wire_records(self) -> None:
        """Representative records are byte-shaped like both source implementations."""
        values = [
            {
                "schema_version": 2,
                "sequence": 0,
                "host_monotonic_ns": 1,
                "host_unix_ns": 2,
                "record_type": "capture_started",
                "capture_id": "old",
                "attributes": {},
            },
            {
                "schema_version": 2,
                "sequence": 1,
                "host_monotonic_ns": 3,
                "host_unix_ns": 4,
                "record_type": "marker",
                "marker_id": "id",
                "marker_kind": "kind",
                "attributes": {},
                "source_time_ns": None,
                "source_clock": None,
            },
            {
                "schema_version": 2,
                "sequence": 2,
                "host_monotonic_ns": 5,
                "host_unix_ns": 6,
                "record_type": "capture_stopped",
                "reason": "normal_completion",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.capture.jsonl.zst"
            path.write_bytes(
                zstd.compress(
                    b"".join((json.dumps(value) + "\n").encode() for value in values)
                )
            )
            records = list(CaptureLogReader(path))
            marker = records[1]
            assert isinstance(marker, Marker)
            self.assertEqual(marker.marker_id, "id")
            self.assertEqual(path.read_bytes(), path.read_bytes())

    def test_invalid_attributes_rejected_at_write_and_decode_boundaries(self) -> None:
        for invalid in (
            {"nested": {"x": 1}},
            {"list": [1]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(CaptureDecodeError):
                validate_attributes(invalid)
        for invalid in ({"nan": float("nan")}, {"infinite": float("inf")}):
            with (
                self.subTest(invalid=invalid),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "x.capture.jsonl.zst"
                with self.assertRaises(CaptureDecodeError):
                    CaptureLogWriter(path, "x", invalid)

    def test_invalid_records_and_lifecycle_are_rejected(self) -> None:
        with self.assertRaises(CaptureDecodeError):
            decode_record({"record_type": "raw_packet"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.capture.jsonl.zst"
            writer = CaptureLogWriter(path, "x")
            writer.start_segment("a", "kind")
            with self.assertRaises(CaptureLifecycleError):
                writer.start_segment("a", "kind")
            with self.assertRaises(CaptureLifecycleError):
                writer.stop_segment("missing")
            with self.assertRaises(CaptureLifecycleError):
                writer.close()
            writer.stop_segment("a")
            writer.close()


if __name__ == "__main__":
    unittest.main()
