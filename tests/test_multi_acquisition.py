"""Hardware-free integration tests for fixed multi-device recording."""

import multiprocessing
import pickle
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from multiprocessing.synchronize import Semaphore
from pathlib import Path
from unittest.mock import patch

import duckdb
import numpy as np

from sifi_streamer.acquisition.backend import create_capture_runtime
from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.ipc import Ready
from sifi_streamer.capture.events import CaptureEvent, DeviceInfoEvent
from sifi_streamer.capture.transport import MessageChannel
from sifi_streamer.exceptions import AckError, RecordingError


@dataclass(frozen=True, slots=True)
class CounterPacket:
    stream_id: str | None
    timestamps: tuple[int, ...]
    data: dict[str, tuple[int | None, ...]]
    validity: dict[str, tuple[bool, ...]]
    reported_rate_hz: float | None = 100.0
    samples_lost: int = 0
    status: str = "ok"

    def capture_document(self) -> None:
        return None


class CounterDevice:
    """Emit batches, including a valid null, with a final packet during stop."""

    def __init__(
        self,
        *,
        fail_after: int | None = None,
        malformed: bool = False,
        fail_connect: bool = False,
    ) -> None:
        self._stop = threading.Event()
        self._count = 0
        self._fail_after = fail_after
        self._malformed = malformed
        self._fail_connect = fail_connect
        self._sink: Callable[[CaptureEvent], None] | None = None

    def set_capture_event_sink(self, sink: Callable[[CaptureEvent], None]) -> None:
        self._sink = sink

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        return (
            SignalStreamSpec(
                "signal",
                (SignalChannelSpec("channel", unit="counts"),),
                100,
                dtype=np.int64,
                source_clock="device counter",
                timestamp_unit="nanoseconds",
                timestamp_dtype=np.int64,
            ),
        )

    @property
    def device_info(self) -> dict[str, object]:
        return {"counter": self._count}

    def connect(self) -> None:
        if self._fail_connect:
            raise RuntimeError("connect exploded")

    def request_stop(self) -> None:
        self._stop.set()

    def disconnect(self) -> None:
        if self._sink is not None:
            self._sink(DeviceInfoEvent("disconnected", self.device_info))

    def read_packet(self) -> CounterPacket:
        self._stop.wait(0.005)
        self._count += 1
        if self._fail_after is not None and self._count > self._fail_after:
            raise LookupError("unexpected device failure")
        return CounterPacket(
            "signal",
            (2**60 + self._count * 2, 2**60 + self._count * 2 + 1),
            {"channel": (2**60 + self._count, None)},
            {"channel": (True,) if self._malformed else (True, True)},
            reported_rate_hz=101.5,
            samples_lost=3,
            status="device_warning",
        )


class PausedMetadataDevice(CounterDevice):
    """Pause while a multi-megabyte Ready message is only partly copied."""

    def __init__(self, mid_copy: Semaphore) -> None:
        super().__init__()
        self._mid_copy = mid_copy

    @property
    def device_info(self) -> dict[str, object]:
        return {"blob": "x" * (3 << 20)}

    def connect(self) -> None:
        original = MessageChannel._write_frame
        signal = self._mid_copy

        def write(channel: MessageChannel[object], frame: bytes, slots: int) -> None:
            message = pickle.loads(frame[8:])
            if isinstance(message, Ready):
                view = memoryview(channel.buffer).cast("B")
                start = channel._write_slot * channel.slot_bytes
                view[start : start + 64] = frame[:64]
                signal.release()
                time.sleep(30)
            original(channel, frame, slots)

        patch.object(MessageChannel, "_write_frame", write).start()


class LargeRegistryDevice(CounterDevice):
    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        return (
            SignalStreamSpec(
                "signal", (SignalChannelSpec("channel"),), 100, label="large" * 4096
            ),
        )


class MultiAcquisitionTests(unittest.TestCase):
    def test_killed_worker_mid_ready_metadata_never_blocks_parent(self) -> None:
        context = multiprocessing.get_context("spawn")
        mid_copy = context.Semaphore(0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mid-ready.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=2),
                partial(PausedMetadataDevice, mid_copy),
            )
            handle.prepare_capture(path, "mid-ready", {}, {})
            errors: list[BaseException] = []

            def enter() -> None:
                try:
                    handle.__enter__()
                except BaseException as exc:
                    errors.append(exc)

            startup = threading.Thread(target=enter, daemon=True)
            startup.start()
            try:
                self.assertTrue(
                    mid_copy.acquire(timeout=10), "worker never copied Ready"
                )
                worker = handle._workers["device"]
                worker.process.terminate()
                worker.process.join(2)
                startup.join(5)
                self.assertFalse(
                    startup.is_alive(), "parent blocked on a partial Ready frame"
                )
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], AckError)
            finally:
                for worker in handle._workers.values():
                    if worker.process.is_alive():
                        worker.process.terminate()
                        worker.process.join(2)
                startup.join(5)

    def test_oversized_ready_metadata_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-ready.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(
                    max_submission_bytes=4096, producer_buffer_bytes=64 << 10
                ),
                LargeRegistryDevice,
            )
            handle.prepare_capture(path, "large-ready", {}, {})
            with self.assertRaisesRegex(AckError, "maximum|exceeds"):
                handle.__enter__()
            self.assertFalse(
                any(worker.process.is_alive() for worker in handle._workers.values())
            )

    def test_killed_recorder_stops_devices_without_client_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recorder-death.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=2),
                {"a": CounterDevice, "b": CounterDevice},
            )
            handle.prepare_capture(path, "recorder-death", {}, {})
            handle.__enter__()
            assert handle._recorder is not None
            handle._recorder._process.terminate()
            handle._recorder._process.join(2)
            deadline = time.monotonic() + 6
            while any(worker.process.is_alive() for worker in handle._workers.values()):
                if time.monotonic() > deadline:
                    self.fail("recorder death did not stop all producers")
                time.sleep(0.02)
            with self.assertRaises(RecordingError):
                handle.stop_capture()

    def test_killed_device_stops_other_devices_and_leaves_incomplete_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device-death.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=2),
                {"a": CounterDevice, "b": CounterDevice},
            )
            handle.prepare_capture(path, "device-death", {}, {})
            handle.__enter__()
            worker = handle._workers["a"]
            worker.process.terminate()
            worker.process.join(2)
            deadline = time.monotonic() + 6
            while any(w.process.is_alive() for w in handle._workers.values()):
                if time.monotonic() > deadline:
                    self.fail("device death did not stop the other producer")
                time.sleep(0.02)
            with self.assertRaises(RecordingError):
                handle.stop_capture()
            with duckdb.connect(str(path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM capture_stopped"
                    ).fetchall(),
                    [(0,)],
                )

    def test_two_devices_keep_scoped_streams_and_exact_sample_only_batches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multi.capture.duckdb"
            runtime = create_capture_runtime(
                path, "multi", {"a": CounterDevice, "b": CounterDevice}
            )
            runtime.controller.start()
            runtime.controller.start_segment("segment", "test")
            time.sleep(0.05)
            self.assertEqual(
                {info.stream_id for info in runtime.monitor.streams},
                {"a/signal", "b/signal"},
            )
            runtime.controller.close()
            with duckdb.connect(str(path), read_only=True) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT DISTINCT reported_rate_hz,samples_lost,status "
                        "FROM packets"
                    ).fetchall(),
                    [(101.5, 3, "device_warning")],
                )
                streams = connection.execute(
                    "SELECT device_id,table_name FROM streams ORDER BY device_id"
                ).fetchall()
                self.assertEqual([row[0] for row in streams], ["a", "b"])
                for device_id, table in streams:
                    rows = connection.execute(
                        f'SELECT source_time,value_0,valid_0 FROM "{table}" '
                        "ORDER BY event_sequence,sample_index"
                    ).fetchall()
                    self.assertGreaterEqual(len(rows), 2)
                    self.assertEqual(rows[0], (2**60 + 2, 2**60 + 1, True))
                    self.assertEqual(rows[1], (2**60 + 3, None, True))
                    # Final disconnect reports are submitted before completion.
                    final_count = runtime.monitor.device_infos[device_id]["counter"]
                    self.assertIsInstance(final_count, int)
                    assert isinstance(final_count, int)
                    self.assertEqual(final_count * 2, len(rows))
                end = connection.execute(
                    "SELECT stop_sequence FROM segments WHERE segment_id='segment'"
                ).fetchall()[0][0]
                maximum = connection.execute(
                    "SELECT max(event_sequence) FROM packets"
                ).fetchall()[0][0]
                self.assertGreater(end, maximum)

    def test_unhandled_device_exception_stops_every_worker_without_monitor_polling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=3),
                {
                    "a": partial(CounterDevice, fail_after=2),
                    "b": CounterDevice,
                },
            )
            handle.prepare_capture(path, "failed", {}, {})
            handle.__enter__()
            deadline = time.monotonic() + 5
            while any(worker.process.is_alive() for worker in handle._workers.values()):
                if time.monotonic() > deadline:
                    self.fail("failure did not stop the other device")
                time.sleep(0.02)
            with self.assertRaises(RecordingError):
                handle.stop_capture()
            with duckdb.connect(str(path), read_only=True) as connection:
                self.assertEqual(
                    connection.execute("SELECT reason FROM capture_stopped").fetchall(),
                    [("acquisition_failure",)],
                )

    def test_bad_shapes_fail_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shape.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=3),
                partial(CounterDevice, malformed=True),
            )
            handle.prepare_capture(path, "shape", {}, {})
            handle.__enter__()
            deadline = time.monotonic() + 5
            while handle.poll_fatal() is None:
                if time.monotonic() > deadline:
                    self.fail("malformed packet did not fail capture")
                time.sleep(0.02)
            with self.assertRaises(RecordingError):
                handle.stop_capture()

    def test_failed_connect_leaves_database_and_cleans_other_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.capture.duckdb"
            handle = BackgroundHandle(
                StreamerConfig(shutdown_timeout_s=3),
                {
                    "a": partial(CounterDevice, fail_connect=True),
                    "b": CounterDevice,
                },
            )
            handle.prepare_capture(path, "startup", {}, {})
            with self.assertRaisesRegex(Exception, "connect exploded"):
                handle.__enter__()
            self.assertTrue(path.exists())
            self.assertFalse(
                any(w.process.is_alive() for w in handle._workers.values())
            )
            with duckdb.connect(str(path), read_only=True) as connection:
                self.assertEqual(
                    connection.execute("SELECT reason FROM capture_stopped").fetchall(),
                    [("startup_failure",)],
                )
