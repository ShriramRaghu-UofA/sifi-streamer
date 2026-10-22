"""Recorder process, timer commits, producer barriers, and bounded transport."""

import multiprocessing as mp
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from typing import Protocol
from unittest.mock import patch

import duckdb

from sifi_streamer.acquisition.devices import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.capture.recorder import RecorderHandle
from sifi_streamer.capture.transport import (
    MessageChannel,
    ProducerEndpoint,
    RecorderConfig,
)
from sifi_streamer.exceptions import RecordingError


class NotificationPipe(Protocol):
    def send(self, value: object) -> None: ...


def pause_during_publication(
    endpoint: ProducerEndpoint, notification: NotificationPipe, after_copy: bool
) -> None:
    original = ProducerEndpoint._write_frame

    def pause(producer: ProducerEndpoint, frame: bytes, slots: int) -> None:
        if after_copy:
            original(producer, frame, slots)
        else:
            view = memoryview(producer.buffer).cast("B")
            view[: len(frame) // 2] = frame[: len(frame) // 2]
        notification.send("copied" if after_copy else "partial copy")
        time.sleep(60)

    with patch.object(ProducerEndpoint, "_write_frame", pause):
        endpoint.send_packet(
            stream_id=None,
            timestamps=(),
            data={},
            validity={},
            document={"large": "x" * 100_000},
        )


def send_packets(endpoint: ProducerEndpoint) -> None:
    for index in range(20):
        endpoint.send_packet(
            stream_id="emg",
            timestamps=(index, index + 0.5),
            data={"value": (index, None)},
            validity={"value": (True, True)},
            document=None if endpoint.device_id == "b" else {"counter": index},
            reported_rate_hz=200.0,
            samples_lost=2,
            status="notice",
            received_monotonic_ns=1000 + index,
            received_unix_ns=2000 + index,
        )
    endpoint.complete()


def abandon_recorder(path: Path, notification: NotificationPipe) -> None:
    handle = RecorderHandle(
        path,
        "abandoned",
        config=RecorderConfig(
            commit_interval_s=0.05,
        ),
    )
    handle.start()
    handle.add_marker("committed", "test")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = handle.poll_status()
        if status is not None and status.committed_events == 1:
            notification.send("committed")
            os._exit(0)
        time.sleep(0.01)
    raise TimeoutError("owner did not observe committed marker")


class RecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.capture.duckdb"

    def recorder(self, config: RecorderConfig | None = None) -> RecorderHandle:
        handle = RecorderHandle(
            self.path,
            "test",
            producer_devices={
                "foreground": None,
                "device:a": "a",
                "device:b": "b",
            },
            config=config,
        )
        self.addCleanup(handle.abort)
        return handle

    def declare(self, handle: RecorderHandle) -> None:
        spec = SignalStreamSpec("emg", (SignalChannelSpec("value"),), 100)
        for device in ("a", "b"):
            handle.register_device(device, "synthetic")
            handle.register_stream(device, spec)
        handle.seal_registration()

    def test_multiple_producers_drain_final_packets_before_close(self) -> None:
        handle = self.recorder()
        handle.start()
        self.declare(handle)
        context = mp.get_context("spawn")
        processes = [
            context.Process(target=send_packets, args=(handle.producer(name),))
            for name in ("device:a", "device:b")
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            process.close()
        sequence = handle.add_marker("end", "operator")
        self.assertGreater(sequence, 0)
        handle.stop()
        final_status = handle.poll_status()
        self.assertIsNotNone(final_status)
        assert final_status is not None
        self.assertEqual(final_status.accepted_events, 41)
        self.assertEqual(final_status.committed_events, 41)
        self.assertEqual(final_status.pending_bytes, 0)
        with duckdb.connect(str(self.path), read_only=True) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM packets").fetchall(), [(40,)]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT reported_rate_hz, samples_lost, status "
                    "FROM packets"
                ).fetchall(),
                [(200.0, 2, "notice")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM packets "
                    "WHERE device_id='b' AND payload IS NULL"
                ).fetchall(),
                [(20,)],
            )
            for table in ("samples_0000", "samples_0001"):
                self.assertEqual(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchall(),
                    [(40,)],
                )
            self.assertEqual(
                connection.execute("SELECT outcome FROM capture_status").fetchall(),
                [("completed",)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT device_id, count(*) FROM producers "
                    "WHERE device_id IS NOT NULL GROUP BY device_id ORDER BY device_id"
                ).fetchall(),
                [("a", 1), ("b", 1)],
            )

    def test_idle_commit_survives_abrupt_recorder_termination(self) -> None:
        handle = self.recorder(RecorderConfig(commit_interval_s=0.05))
        handle.start()
        handle.add_marker("queued", "test")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = handle.poll_status()
            if status is not None and status.committed_events == 1:
                break
            time.sleep(0.01)
        else:
            self.fail("idle recorder did not commit on its independent deadline")
        handle._process.terminate()
        handle._process.join(5)
        handle.abort()
        # Read/write reopening performs WAL recovery after forced process death.
        with duckdb.connect(str(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT marker_id FROM markers").fetchall(),
                [("queued",)],
            )
            self.assertEqual(
                connection.execute("SELECT outcome FROM capture_status").fetchall(),
                [("incomplete",)],
            )

    def test_recorder_aborts_when_its_launcher_exits_abruptly(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        owner = context.Process(target=abandon_recorder, args=(self.path, child))
        owner.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "committed")
            owner.join(5)
            self.assertFalse(owner.is_alive())
            self.assertEqual(owner.exitcode, 0)
            deadline = time.monotonic() + 10
            while True:
                try:
                    connection = duckdb.connect(str(self.path))
                except duckdb.IOException:
                    if time.monotonic() >= deadline:
                        self.fail("orphan recorder did not release the database")
                    time.sleep(0.02)
                else:
                    break
            with connection:
                self.assertEqual(
                    connection.execute("SELECT marker_id FROM markers").fetchall(),
                    [("committed",)],
                )
                self.assertEqual(
                    connection.execute("SELECT outcome FROM capture_status").fetchall(),
                    [("incomplete",)],
                )
        finally:
            if owner.is_alive():
                owner.terminate()
                owner.join(5)
            owner.close()
            parent.close()

    def test_maximum_submission_fails_without_silent_drop(self) -> None:
        handle = self.recorder(
            RecorderConfig(max_submission_bytes=1024, producer_buffer_bytes=4096)
        )
        producer = handle.producer("device:a")
        with self.assertRaisesRegex(RecordingError, "maximum"):
            producer.send_packet(
                stream_id=None,
                timestamps=(),
                data={},
                validity={},
                document={"large": "x" * 2048},
            )
        self.assertTrue(producer.failed)
        self.assertEqual(producer.pending_bytes, 0)

    def test_budget_includes_submissions_waiting_for_queue_feeder(self) -> None:
        handle = self.recorder(
            RecorderConfig(
                max_submission_bytes=1024,
                producer_buffer_bytes=1024,
                enqueue_timeout_s=0.05,
            )
        )
        producer = handle.producer("device:a")
        count = 0
        with self.assertRaisesRegex(RecordingError, "deadline"):
            while True:
                producer.send_packet(
                    stream_id=None,
                    timestamps=(),
                    data={},
                    validity={},
                    document={"counter": count},
                )
                count += 1
        self.assertGreater(count, 0)
        self.assertLessEqual(producer.pending_bytes, 1024)

    def test_partial_credit_acquisition_is_returned_on_timeout(self) -> None:
        handle = self.recorder(
            RecorderConfig(
                max_submission_bytes=100_000,
                producer_buffer_bytes=131_072,
                enqueue_timeout_s=0.05,
            )
        )
        producer = handle.producer("device:a")
        producer.send_packet(
            stream_id=None,
            timestamps=(),
            data={},
            validity={},
            document={"small": True},
        )
        with self.assertRaisesRegex(RecordingError, "deadline"):
            producer.send_packet(
                stream_id=None,
                timestamps=(),
                data={},
                validity={},
                document={"large": "x" * 70_000},
            )
        self.assertTrue(producer.free_slots.acquire(False))
        producer.free_slots.release()

    def test_killed_producer_cannot_publish_partial_or_unannounced_frame(self) -> None:
        context = mp.get_context("spawn")
        for after_copy in (False, True):
            with self.subTest(after_copy=after_copy):
                path = (
                    Path(self.directory.name) / f"partial-{after_copy}.capture.duckdb"
                )
                handle = RecorderHandle(
                    path,
                    "partial",
                    producer_devices={
                        "foreground": None,
                        "device:a": "a",
                    },
                    config=RecorderConfig(
                        max_submission_bytes=128 << 10,
                        producer_buffer_bytes=256 << 10,
                        shutdown_timeout_s=0.1,
                    ),
                )
                self.addCleanup(handle.abort)
                handle.start()
                handle.register_device("a")
                handle.seal_registration()
                parent, child = context.Pipe()
                process = context.Process(
                    target=pause_during_publication,
                    args=(handle.producer("device:a"), child, after_copy),
                )
                process.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(10))
                    parent.recv()
                    process.terminate()
                    process.join(5)
                    self.assertFalse(process.is_alive())
                    # No reader mutex or partial-pipe read can strand this command.
                    handle.add_marker("responsive", "test")
                    with self.assertRaisesRegex(RecordingError, "deadline"):
                        handle.stop()
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                    process.close()
                    parent.close()

    def test_inconsistent_packet_fails_recorder(self) -> None:
        handle = self.recorder()
        handle.start()
        self.declare(handle)
        handle.producer("device:a").send_packet(
            stream_id="emg",
            timestamps=(1, 2),
            data={"value": (1,)},
            validity={"value": (True,)},
            document=None,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            failure = handle.poll_failure()
            if failure is not None:
                self.assertIn("length", failure.message)
                break
            time.sleep(0.01)
        else:
            self.fail("malformed packet did not fail recorder")

    def test_registration_after_seal_fails_the_capture(self) -> None:
        handle = self.recorder()
        handle.start()
        handle.seal_registration()
        with self.assertRaisesRegex(RecordingError, "sealed"):
            handle.register_device("late")

    def test_stop_waits_for_producer_completion_and_times_out_explicitly(self) -> None:
        handle = self.recorder(RecorderConfig(shutdown_timeout_s=0.1))
        handle.start()
        with self.assertRaisesRegex(RecordingError, "deadline"):
            handle.stop()

    def test_shutdown_closes_open_segments_after_final_packets(self) -> None:
        handle = self.recorder()
        handle.start()
        self.declare(handle)
        handle.start_segment("outer", "test")
        handle.start_segment("inner", "test")
        send_packets(handle.producer("device:a"))
        handle.producer("device:b").complete()
        handle.stop("operator_interrupt")
        with duckdb.connect(str(self.path), read_only=True) as connection:
            rows = connection.execute(
                "SELECT segment_id, stop_sequence, reason FROM segments "
                "ORDER BY stop_sequence"
            ).fetchall()
            self.assertEqual([row[0] for row in rows], ["inner", "outer"])
            last_packet = connection.execute(
                "SELECT max(event_sequence) FROM packets"
            ).fetchall()[0][0]
            self.assertTrue(all(row[1] > last_packet for row in rows))


class MessageChannelTests(unittest.TestCase):
    def test_full_channel_wraps_without_losing_or_reordering_metadata(self) -> None:
        channel = MessageChannel[str].create(
            mp.get_context("spawn"),
            capacity_bytes=4096,
            max_message_bytes=1024,
            timeout_s=0.05,
        )
        for index in range(channel.slot_count):
            channel.put_nowait(str(index))
        with self.assertRaises(queue.Full):
            channel.put_nowait("full")
        self.assertEqual(channel.get_nowait(), "0")
        channel.put_nowait("wrapped")
        expected = [str(index) for index in range(1, channel.slot_count)] + ["wrapped"]
        self.assertEqual([channel.get_nowait() for _ in expected], expected)
        with self.assertRaises(queue.Empty):
            channel.get(timeout=0.01)

    def test_partial_credit_timeout_does_not_damage_previous_message(self) -> None:
        channel = MessageChannel[str].create(
            mp.get_context("spawn"),
            capacity_bytes=131_072,
            max_message_bytes=100_000,
            timeout_s=0.01,
        )
        channel.put("small")
        with self.assertRaises(queue.Full):
            channel.put("x" * 70_000)
        self.assertEqual(channel.get_nowait(), "small")
        channel.put("y" * 70_000)
        self.assertEqual(channel.get_nowait(), "y" * 70_000)

    def test_oversized_metadata_rejected_before_reserving_capacity(self) -> None:
        channel = MessageChannel[str].create(
            mp.get_context("spawn"),
            capacity_bytes=4096,
            max_message_bytes=1024,
        )
        with self.assertRaisesRegex(RecordingError, "maximum"):
            channel.put("x" * 2048)
        channel.put("valid")
        self.assertEqual(channel.get_nowait(), "valid")


if __name__ == "__main__":
    unittest.main()
