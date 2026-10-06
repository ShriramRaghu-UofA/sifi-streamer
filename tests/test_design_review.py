import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from sifi_streamer.acquisition.backend import AcquisitionCaptureBackend
from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.ipc import Ready, StreamInfo
from sifi_streamer.acquisition.reader import SharedMemoryReader
from sifi_streamer.acquisition.worker.acquisition import AcquisitionThread
from sifi_streamer.capture import (
    CaptureController,
    CaptureDecodeError,
    CaptureLogReader,
    CaptureLogWriter,
    run_timed_capture,
)
from sifi_streamer.exceptions import AckTimeoutError, DeviceError
from sifi_streamer.sifi.bridge import SiFiBridgeDevice
from sifi_streamer.sifi.devices import SyntheticSiFiDevice
from sifi_streamer.sifi.sensor_profile import sensor_profile_from_dict


class DesignReviewTests(unittest.TestCase):
    def test_backend_interrupted_start_releases_handle(self) -> None:
        handle = MagicMock()
        handle.start_capture.side_effect = KeyboardInterrupt
        backend = AcquisitionCaptureBackend(
            StreamerConfig(),
            SyntheticSiFiDevice,
            Path("unused"),
            "capture",
            handle_factory=lambda *_: handle,
        )
        with self.assertRaises(KeyboardInterrupt):
            backend.start()
        backend.stop()
        handle.__exit__.assert_called_once()

    def test_udp_reader_failure_runs_bridge_cleanup(self) -> None:
        reader = Mock()
        reader.connect.side_effect = DeviceError("UDP bind failed")
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "bridge"
            executable.touch()
            device = SiFiBridgeDevice(executable=executable, transport="udp")
            with (
                patch.object(device, "_make_reader", return_value=reader),
                self.assertRaisesRegex(DeviceError, "UDP bind failed"),
            ):
                device.connect()
        reader.disconnect.assert_called_once()
        self.assertIsNone(device._reader)

    def test_bridge_shutdown_closes_stdin_after_write_failure(self) -> None:
        device = SiFiBridgeDevice()
        process = MagicMock()
        process.stdin.write.side_effect = OSError("pipe closed")
        device._process = process
        device.disconnect()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_nonfinite_duration_rejected_before_backend_start(self) -> None:
        backend = Mock()
        for duration in (float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                run_timed_capture(CaptureController(backend), duration)
        backend.start.assert_not_called()

    def test_backend_stop_failure_is_not_retried(self) -> None:
        handle = MagicMock()
        backend = AcquisitionCaptureBackend(
            StreamerConfig(),
            SyntheticSiFiDevice,
            Path("unused"),
            "capture",
            handle_factory=lambda *_: handle,
        )
        backend.start()
        handle.stop_capture.side_effect = OSError("stop failed")
        with self.assertRaisesRegex(OSError, "stop failed"):
            backend.stop()
        backend.stop()
        handle.stop_capture.assert_called_once()
        handle.__exit__.assert_called_once()

    def test_invalid_capture_id_does_not_create_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.capture.jsonl.zst"
            with self.assertRaises(CaptureDecodeError):
                CaptureLogWriter(path, "")
            self.assertFalse(path.exists())

    def test_failed_writer_initialization_closes_file(self) -> None:
        file = Mock()
        with (
            patch.object(Path, "open", return_value=file),
            patch.object(
                CaptureLogWriter, "_append", side_effect=OSError("disk failure")
            ),
            self.assertRaisesRegex(OSError, "disk failure"),
        ):
            CaptureLogWriter(Path("unused"), "capture")
        file.close.assert_called_once()

    def test_invalid_segment_stop_preserves_active_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.capture.jsonl.zst"
            with CaptureLogWriter(path, "capture") as writer:
                writer.start_segment("segment", "kind")
                with (
                    patch(
                        "sifi_streamer.capture.records.encode_record",
                        side_effect=CaptureDecodeError("invalid record"),
                    ),
                    self.assertRaises(CaptureDecodeError),
                ):
                    writer.stop_segment("segment", "completed")
                writer.stop_segment("segment", "completed")
            self.assertEqual(len(list(CaptureLogReader(path))), 4)

    def test_thread_can_be_joined_after_stopping(self) -> None:
        stopped = threading.Event()
        stopped.set()
        thread = AcquisitionThread(SyntheticSiFiDevice(), lambda _: None, stopped)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(thread.failure)

    def test_handle_partial_attachment_closes_readers_and_worker(self) -> None:
        context = Mock()
        process = context.Process.return_value
        process.is_alive.return_value = False
        first_reader = Mock()
        ready = Ready(
            tuple(
                StreamInfo(name, name, 16, ("value",), 10, "<f4")
                for name in ("first", "second")
            ),
            None,
        )
        with (
            patch(
                "sifi_streamer.acquisition.handle.multiprocessing.get_context",
                return_value=context,
            ),
            patch(
                "sifi_streamer.acquisition.handle.SharedMemoryReader",
                side_effect=[first_reader, ValueError("bad layout")],
            ),
        ):
            handle = BackgroundHandle(StreamerConfig(), SyntheticSiFiDevice)
            with (
                patch.object(handle, "_wait_ack", return_value=ready),
                self.assertRaisesRegex(ValueError, "bad layout"),
            ):
                handle.__enter__()
            first_reader.close.assert_called_once()
            process.join.assert_called_once_with(timeout=5)
            self.assertFalse(handle._entered)
            self.assertEqual(handle._stream_readers, {})

    def test_handle_startup_timeout_terminates_worker(self) -> None:
        context = Mock()
        process = context.Process.return_value
        process.is_alive.return_value = True
        with patch(
            "sifi_streamer.acquisition.handle.multiprocessing.get_context",
            return_value=context,
        ):
            handle = BackgroundHandle(StreamerConfig(), SyntheticSiFiDevice)
            with (
                patch.object(handle, "_wait_ack", return_value=None),
                self.assertRaises(AckTimeoutError),
            ):
                handle.__enter__()
            process.terminate.assert_called_once()
            self.assertFalse(handle._entered)

    def test_reader_releases_attachment_after_invalid_layout(self) -> None:
        attachment = Mock()
        with (
            patch(
                "sifi_streamer.acquisition.reader.SharedMemory", return_value=attachment
            ),
            patch(
                "sifi_streamer.acquisition.reader.SeqlockRingBuffer",
                side_effect=ValueError("bad layout"),
            ),
            self.assertRaisesRegex(ValueError, "bad layout"),
        ):
            SharedMemoryReader("stream", 0, 1)
        attachment.close.assert_called_once()

    def test_bridge_missing_startup_state_is_a_device_error(self) -> None:
        device = SiFiBridgeDevice()
        with self.assertRaisesRegex(DeviceError, "device info"):
            device._validate_sensor_capabilities()
        with self.assertRaisesRegex(DeviceError, "TCP packet reader"):
            device._connect_tcp_when_ready()

    def test_config_rejects_nonfinite_durations(self) -> None:
        for field, create in (
            (
                "ring_buffer_seconds",
                lambda value: StreamerConfig(ring_buffer_seconds=value),
            ),
            ("ack_timeout_s", lambda value: StreamerConfig(ack_timeout_s=value)),
            (
                "capture_flush_interval_s",
                lambda value: StreamerConfig(capture_flush_interval_s=value),
            ),
        ):
            for value in (float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, "finite and positive"),
                ):
                    create(value)

    def test_profile_rejects_mixed_key_types_diagnostically(self) -> None:
        with self.assertRaisesRegex(ValueError, "keys must be strings"):
            sensor_profile_from_dict({1: None, "unexpected": None})
