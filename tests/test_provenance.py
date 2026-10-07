"""Capture provenance, explicit reporting, and failure persistence."""

import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sifi_streamer.acquisition.backend import create_capture_runtime
from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.ipc import DeviceInfoUpdate
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
from sifi_streamer.capture import (
    CaptureDecodeError,
    CaptureLogReader,
    CaptureLogWriter,
    CaptureStopped,
    DeviceInfo,
    DeviceInfoEvent,
    Diagnostic,
    DiagnosticEvent,
    LaunchConfiguration,
    RawPacket,
    decode_record,
    encode_record,
)
from sifi_streamer.capture.events import CaptureEventSink
from sifi_streamer.exceptions import CaptureInitializationError
from sifi_streamer.sifi.bridge import SiFiBridgeDevice
from sifi_streamer.sifi.devices import SiFiPacket, SyntheticSiFiDevice
from sifi_streamer.web.cli import sifi_device_summary
from tests.test_sensor_profile import ALL_SENSOR_INFO


class ReportingDevice(SyntheticSiFiDevice):
    """A test integration emitting staged and runtime metadata."""

    def set_capture_event_sink(self, sink: CaptureEventSink) -> None:
        self.sink = sink

    def connect(self) -> None:
        self.sink(DeviceInfoEvent("before_configuration", {"name": "before"}))
        self.sink(DeviceInfoEvent("after_configuration", {"name": "configured"}))
        super().connect()
        self.sink(DeviceInfoEvent("after_start", {"name": "running"}))
        self.reported = False

    def read_packet(self):
        if not self.reported:
            self.reported = True
            self.sink(DeviceInfoEvent("during_capture", {}))
            self.sink(
                DiagnosticEvent(
                    "warning",
                    "test-device",
                    "during_capture",
                    "quality",
                    "Check placement",
                    {"channels": [1]},
                )
            )
        return super().read_packet()


class FailingDevice(ReportingDevice):
    def connect(self) -> None:
        self.sink(DeviceInfoEvent("before_configuration", {"name": "failed-device"}))
        raise RuntimeError("configuration rejected")


class FailingAcquisitionDevice(SyntheticSiFiDevice):
    def read_packet(self):
        raise RuntimeError("reader failed")


class ProvenanceTests(unittest.TestCase):
    def test_queued_old_reports_cannot_replace_ready_snapshot(self) -> None:
        handle = BackgroundHandle(StreamerConfig(), SyntheticSiFiDevice)
        handle._device_info = {"name": "ready"}
        handle._device_info_revision = 3
        updates = queue.Queue()
        with patch.object(handle, "_device_info_queue", updates):
            updates.put(DeviceInfoUpdate(1, {"name": "old"}))
            self.assertEqual(handle.device_info, {"name": "ready"})
            updates.put(DeviceInfoUpdate(4, {}))
            self.assertEqual(handle.device_info, {})

    def test_delayed_capture_preserves_report_and_diagnostic_order(self) -> None:
        recorder = RecorderFSM(StreamerConfig(), None)
        recorder.record_event(DeviceInfoEvent("before_configuration", {}))
        recorder.record_event(
            DiagnosticEvent(
                "warning", "device", "configuration", "quality", "Check setup", {}
            )
        )
        recorder.record_event(DeviceInfoEvent("after_start", {"name": "ready"}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delayed.capture.jsonl.zst"
            recorder.start_capture(path, "delayed")
            recorder.stop_capture()
            records = list(CaptureLogReader(path))
        self.assertEqual(
            [r.record_type for r in records],
            [
                "capture_started",
                "launch_configuration",
                "device_info",
                "diagnostic",
                "device_info",
                "capture_stopped",
            ],
        )

    def test_launch_configuration_is_exclusive_and_second(self) -> None:
        from sifi_streamer.capture import CaptureLifecycleError

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launch.capture.jsonl.zst"
            with CaptureLogWriter(path, "launch") as writer:
                writer.append_launch_configuration({})
                with self.assertRaises(CaptureLifecycleError):
                    writer.append_launch_configuration({})
            self.assertEqual(len(list(CaptureLogReader(path))), 3)

    def test_manual_report_updates_monitor_and_is_retained_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.capture.jsonl.zst"
            runtime = create_capture_runtime(path, "manual", SyntheticSiFiDevice)
            runtime.controller.start()
            runtime.controller.record_event(
                DeviceInfoEvent("during_capture", {"serial": "123"})
            )
            deadline = time.monotonic() + 3
            while runtime.monitor.device_info != {"serial": "123"}:
                if time.monotonic() > deadline:
                    self.fail("new report not delivered")
                time.sleep(0.01)
            runtime.controller.close()
            self.assertEqual(runtime.monitor.device_info, {"serial": "123"})
            self.assertIn("acquisition", runtime.monitor.launch_configuration)

    def test_payload_round_trip_empty_reports_and_diagnostic_severities(self) -> None:
        payload = {"nested": [None, {"vendor": "untouched"}], "rate": 0.1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports.capture.jsonl.zst"
            with CaptureLogWriter(path, "reports") as writer:
                writer.append_launch_configuration(payload)
                writer.append_device_info("after_start", {})
                writer.append_device_info("during_capture", payload)
                writer.append_device_info("during_capture", payload)
                for severity in ("info", "warning", "error"):
                    writer.append_diagnostic(
                        severity,
                        "consumer",
                        "processing",
                        "classification_failure",
                        "Explicit event",
                        payload,
                    )
                writer.append_packet({"vendor": "still recording"})
            records = list(CaptureLogReader(path))
        assert isinstance(records[1], LaunchConfiguration)
        assert isinstance(records[2], DeviceInfo)
        assert isinstance(records[3], DeviceInfo)
        assert isinstance(records[4], DeviceInfo)
        self.assertEqual(records[1].configuration, payload)
        self.assertEqual(records[2].info, {})
        self.assertEqual(records[3].info, records[4].info)
        self.assertEqual(
            [r.severity for r in records if isinstance(r, Diagnostic)],
            ["info", "warning", "error"],
        )
        self.assertIsInstance(records[-2], RawPacket)
        for record in records:
            self.assertEqual(decode_record(json.loads(encode_record(record))), record)

    def test_invalid_documents_and_envelopes_do_not_consume_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.capture.jsonl.zst"
            with CaptureLogWriter(path, "invalid") as writer:
                for document in (
                    {"nested": {1: "bad"}},
                    {"x": float("nan")},
                    {"x": object()},
                    {"x": (1, 2)},
                ):
                    with (
                        self.subTest(document=document),
                        self.assertRaises(CaptureDecodeError),
                    ):
                        writer.append_device_info("during_capture", document)
                with self.assertRaises(CaptureDecodeError):
                    writer.append_device_info("", {})
                with self.assertRaises(CaptureDecodeError):
                    decode_record(
                        {
                            "schema_version": 3,
                            "sequence": 0,
                            "host_monotonic_ns": 0,
                            "host_unix_ns": 0,
                            "record_type": "diagnostic",
                            "severity": "fatal",
                            "source": "device",
                            "stage": "start",
                            "code": "x",
                            "message": "x",
                            "details": {},
                        }
                    )
                writer.append_device_info("after_start", {})
            self.assertEqual([r.sequence for r in CaptureLogReader(path)], [0, 1, 2])

    def test_packet_none_suppresses_record_and_empty_document_is_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.capture.jsonl.zst"
            recorder = RecorderFSM(StreamerConfig(), {})
            recorder.start_capture(path, "empty")
            no_document = Mock()
            no_document.capture_document.return_value = None
            recorder.on_packet(no_document)
            recorder.on_packet(SiFiPacket("event", [], {}, 0, document={}))
            recorder.stop_capture()
            records = list(CaptureLogReader(path))
        self.assertEqual([r.info for r in records if isinstance(r, DeviceInfo)], [{}])
        self.assertEqual([r.packet for r in records if isinstance(r, RawPacket)], [{}])

    def test_failed_startup_records_requested_config_report_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.capture.jsonl.zst"
            runtime = create_capture_runtime(
                path,
                "failed",
                FailingDevice,
                launch_configuration={"requested": {"rate": 100}},
            )
            with self.assertRaises(CaptureInitializationError):
                runtime.controller.start()
            runtime.controller.close()
            records = list(CaptureLogReader(path))
        self.assertEqual(
            [r.record_type for r in records],
            [
                "capture_started",
                "launch_configuration",
                "device_info",
                "diagnostic",
                "capture_stopped",
            ],
        )
        assert isinstance(records[1], LaunchConfiguration)
        assert isinstance(records[2], DeviceInfo)
        assert isinstance(records[3], Diagnostic)
        assert isinstance(records[-1], CaptureStopped)
        self.assertEqual(records[1].configuration["requested"], {"rate": 100})
        self.assertEqual(records[2].stage, "before_configuration")
        self.assertIn("configuration rejected", records[3].message)
        self.assertEqual(records[-1].reason, "startup_failure")

    def test_runtime_reports_and_nonterminal_errors_preserve_recording(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.capture.jsonl.zst"
            runtime = create_capture_runtime(path, "live", ReportingDevice)
            runtime.controller.start()
            details = {"scores": [0.1, 0.2]}
            runtime.controller.record_event(
                DiagnosticEvent(
                    "error",
                    "consumer",
                    "processing",
                    "classification_failure",
                    "No classification",
                    details,
                )
            )
            details["scores"].append(99)
            runtime.controller.marker("still-live", "note")
            time.sleep(0.02)
            self.assertEqual(runtime.monitor.device_info, {})
            runtime.controller.close()
            self.assertEqual(runtime.monitor.device_info, {})
            records = list(CaptureLogReader(path))
        reports = [r for r in records if isinstance(r, DeviceInfo)]
        self.assertEqual(
            [r.stage for r in reports],
            [
                "before_configuration",
                "after_configuration",
                "after_start",
                "during_capture",
            ],
        )
        diagnostic = next(
            r for r in records if isinstance(r, Diagnostic) and r.source == "consumer"
        )
        self.assertEqual(diagnostic.details, {"scores": [0.1, 0.2]})
        self.assertTrue(any(isinstance(r, RawPacket) for r in records))
        assert isinstance(records[-1], CaptureStopped)
        self.assertEqual(records[-1].reason, "normal_completion")

    def test_runtime_failure_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reader.capture.jsonl.zst"
            runtime = create_capture_runtime(path, "reader", FailingAcquisitionDevice)
            runtime.controller.start()
            deadline = time.monotonic() + 3
            while runtime.monitor.fatal() is None:
                if time.monotonic() > deadline:
                    self.fail("worker failure not reported")
                time.sleep(0.01)
            runtime.controller.close("acquisition_failure")
            records = list(CaptureLogReader(path))
        self.assertTrue(
            any(
                isinstance(r, Diagnostic) and r.code == "acquisition_failure"
                for r in records
            )
        )
        self.assertIsInstance(records[-1], CaptureStopped)

    def test_sifi_emits_each_startup_report_without_changing_payload(self) -> None:
        device = SiFiBridgeDevice()
        reports = []
        device.set_capture_event_sink(reports.append)
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "bridge"
            executable.touch()
            device._executable = executable
            with (
                patch.object(device, "_launch"),
                patch.object(device, "_send"),
                patch.object(device, "_wait_for_response"),
                patch.object(device, "_wait_for_info", return_value=ALL_SENSOR_INFO),
                patch.object(device, "_make_reader", return_value=Mock()),
            ):
                device.connect()
                device.disconnect()
        self.assertEqual(
            [r.stage for r in reports],
            ["before_configuration", "after_configuration", "after_start"],
        )
        self.assertTrue(all(r.info == ALL_SENSOR_INFO for r in reports))

    def test_sifi_summary_does_not_mutate_nested_report(self) -> None:
        document = {
            "info": {
                "name": "SiFiBand_AA92",
                "mac": "AA:BB:CC:DD:EE:FF",
                "configuration": {"emg": {"fs": 1600}},
            }
        }
        before = json.dumps(document)
        summary = sifi_device_summary(document)
        self.assertEqual(summary["Name"], "SiFiBand_AA92")
        self.assertEqual(summary["MAC address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(json.dumps(document), before)
