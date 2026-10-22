"""Database capture fidelity, lifecycle, and committed-prefix recovery."""

import hashlib
import multiprocessing as mp
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import duckdb
import numpy as np
import numpy.typing as npt

from sifi_streamer.acquisition.devices import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.capture import (
    Attributes,
    CaptureDecodeError,
    CaptureLifecycleError,
    CaptureLogReader,
    CaptureLogWriter,
    CaptureStarted,
    CaptureStopped,
    DeviceInfo,
    Marker,
    RawPacket,
    SegmentStopped,
    recover_capture,
    validate_attributes,
)


class _Pipe(Protocol):
    def send(self, value: object) -> None: ...
    def recv(self) -> object: ...


def _crash_writer(path: str, pipe: _Pipe) -> None:
    writer = CaptureLogWriter(Path(path), "crash")
    writer.append_marker("committed", "note")
    writer.flush()
    writer.append_marker("pending", "note")
    pipe.send("ready")
    pipe.recv()


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "capture.capture.duckdb"

    def spec(self, *, dtype: npt.DTypeLike = np.float64) -> SignalStreamSpec:
        return SignalStreamSpec(
            "emg",
            (SignalChannelSpec('channel"one', unit="mV"),),
            1000,
            dtype=dtype,
            source_clock="device ticks",
            timestamp_unit="ticks",
            timestamp_dtype=np.int64,
        )

    def test_round_trip_preserves_raw_documents_and_typed_reports(self) -> None:
        document = {"vendor": {"unknown": [True, None, 2**63 - 1]}, "empty": {}}
        with CaptureLogWriter(self.path, "capture", {"operator": "one"}) as writer:
            writer.append_launch_configuration({"rate": 1000})
            writer.append_device_info("connected", document)
            writer.append_packet(document)
            writer.append_marker("occurrence", "button", {"ready": True})
        records = list(CaptureLogReader(self.path))
        self.assertIsInstance(records[0], CaptureStarted)
        self.assertEqual(
            next(r.packet for r in records if isinstance(r, RawPacket)), document
        )
        self.assertEqual(
            next(r.info for r in records if isinstance(r, DeviceInfo)), document
        )
        marker = next(r for r in records if isinstance(r, Marker))
        self.assertEqual(
            (marker.marker_id, marker.marker_kind), ("occurrence", "button")
        )
        self.assertIsInstance(records[-1], CaptureStopped)

    def test_batched_samples_preserve_integer_precision_nulls_and_validity(
        self,
    ) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_device("a")
            writer.register_stream("a", self.spec(dtype=np.int64))
            writer.register_producer("worker-a", "a")
            with writer.submission(
                "worker-a", 0, 123456789012345678, 1792610095983000000
            ):
                sequence = writer.append_packet_data(
                    "a",
                    "emg",
                    [2**60 + 1, 2**60 + 2, 2**60 + 3],
                    {'channel"one': [2**60 + 1, None, None]},
                    {'channel"one': [True, True, False]},
                    None,
                    reported_rate_hz=999.5,
                    samples_lost=17,
                    status="producer_warning",
                )
        reader = CaptureLogReader(self.path)
        samples = reader.query("SELECT * FROM samples_0000 ORDER BY sample_index")
        self.assertEqual(
            samples,
            [
                (sequence, 0, 2**60 + 1, 2**60 + 1, True),
                (sequence, 1, 2**60 + 2, None, True),
                (sequence, 2, 2**60 + 3, None, False),
            ],
        )
        self.assertEqual(
            reader.query(
                "SELECT producer_id,producer_sequence,received_monotonic_ns,"
                "received_unix_ns "
                "FROM events WHERE event_sequence=?",
                [sequence],
            ),
            [("worker-a", 0, 123456789012345678, 1792610095983000000)],
        )
        packet = next(r for r in reader if isinstance(r, RawPacket))
        self.assertIsNone(packet.packet)
        self.assertEqual(
            reader.query("SELECT reported_rate_hz,samples_lost,status FROM packets"),
            [(999.5, 17, "producer_warning")],
        )
        self.assertEqual(reader.streams()[0]["source_clock"], "device ticks")
        self.assertEqual(
            reader.query("SELECT channel_id,column_name FROM channels"),
            [('channel"one', "value_0")],
        )

    def test_stream_ids_are_device_local_and_registration_is_explicit(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_device("a")
            writer.register_device("b")
            writer.register_stream("a", self.spec())
            writer.register_stream("b", self.spec())
            with self.assertRaises(CaptureLifecycleError):
                writer.register_stream("a", self.spec())
            with self.assertRaises(CaptureLifecycleError):
                writer.append_packet_data("a", "undeclared", [], {}, {}, None)
        self.assertEqual(len(CaptureLogReader(self.path).streams()), 2)

    def test_invalid_packet_metadata_is_rejected_before_event_creation(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_device("a")
            for rate in (float("inf"), float("nan"), 0.0, -1.0, True):
                with self.subTest(rate=rate), self.assertRaises(CaptureDecodeError):
                    writer.append_packet_data(
                        "a", None, [], {}, {}, None, reported_rate_hz=rate
                    )
            for lost in (-1, 2**63, True):
                with self.subTest(lost=lost), self.assertRaises(CaptureDecodeError):
                    writer.append_packet_data(
                        "a", None, [], {}, {}, None, samples_lost=lost
                    )
            with self.assertRaises(CaptureDecodeError):
                writer.append_packet_data(
                    "a", None, [], {}, {}, None, status=cast(str, 123)
                )
            self.assertEqual(writer.append_packet_data("a", None, [], {}, {}, None), 1)
        self.assertEqual(
            CaptureLogReader(self.path).query(
                "SELECT reported_rate_hz,samples_lost,status FROM packets"
            ),
            [(None, 0, "ok")],
        )

    def test_packet_shapes_and_declared_numeric_types_are_validated(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_device("a")
            writer.register_stream("a", self.spec(dtype=np.int64))
            for times, data, valid in (
                ([1], {'channel"one': []}, {'channel"one': [True]}),
                ([1], {'channel"one': [1.25]}, {'channel"one': [True]}),
                ([1], {'channel"one': [2**64]}, {'channel"one': [True]}),
                ([1], {'channel"one': [1]}, {'channel"one': [1]}),
            ):
                with self.subTest(data=data), self.assertRaises(CaptureDecodeError):
                    writer.append_packet_data(
                        "a",
                        "emg",
                        times,
                        data,
                        cast(Mapping[str, Sequence[bool]], valid),
                        None,
                    )
        self.assertFalse(
            any(isinstance(r, RawPacket) for r in CaptureLogReader(self.path))
        )

    def test_submission_sequences_reject_gaps_and_preserve_order(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_producer("foreground", role="foreground")
            with writer.submission("foreground", 0, 1, 2):
                writer.append_marker("first", "note")
            with (
                self.assertRaises(CaptureLifecycleError),
                writer.submission("foreground", 2, 3, 4),
            ):
                writer.append_marker("gap", "note")
            with writer.submission("foreground", 1, 3, 4):
                writer.append_marker("second", "note")
        self.assertEqual(
            CaptureLogReader(self.path).query(
                "SELECT producer_sequence FROM events WHERE producer_id='foreground' "
                "ORDER BY event_sequence"
            ),
            [(0,), (1,)],
        )

    def test_producer_registration_and_device_association_are_enforced(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_device("a")
            writer.register_device("b")
            writer.register_producer("worker-a", "a")
            with (
                self.assertRaises(CaptureLifecycleError),
                writer.submission("unknown", 0, 1, 2),
            ):
                writer.append_marker("unknown", "note")
            with (
                self.assertRaises(CaptureLifecycleError),
                writer.submission("worker-a", 0, 1, 2),
            ):
                writer.append_packet_data("b", None, [], {}, {}, {})
            with writer.submission("worker-a", 0, 1, 2):
                writer.append_packet_data("a", None, [], {}, {}, {})
        self.assertEqual(
            CaptureLogReader(self.path).query("SELECT device_id FROM packets"), [("a",)]
        )

    def test_invalid_receipt_clock_rejected_before_changing_sequence(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.register_producer("foreground", role="foreground")
            with (
                self.assertRaises(CaptureDecodeError),
                writer.submission("foreground", 0, 2**63, 1),
            ):
                writer.append_marker("bad", "note")
            with writer.submission("foreground", 0, 1, 2):
                self.assertEqual(writer.append_marker("good", "note"), 1)

    def test_segments_close_reverse_and_segment_ids_can_be_reused(self) -> None:
        with CaptureLogWriter(self.path, "capture") as writer:
            writer.start_segment("outer", "phase")
            writer.start_segment("inner", "phase")
            with self.assertRaises(CaptureLifecycleError):
                writer.stop_segment("outer")
            writer.stop_segment("inner")
            writer.start_segment("inner", "phase")
        stops = [
            r for r in CaptureLogReader(self.path) if isinstance(r, SegmentStopped)
        ]
        self.assertEqual(
            [(r.segment_id, r.reason) for r in stops],
            [("inner", None), ("inner", "completed"), ("outer", "completed")],
        )
        self.assertEqual(
            CaptureLogReader(self.path).query("SELECT count(*) FROM segments"), [(3,)]
        )

    def test_abnormal_close_aborts_remaining_segments(self) -> None:
        writer = CaptureLogWriter(self.path, "capture")
        writer.start_segment("open", "phase")
        writer.close("operator_interrupt")
        writer.close("operator_interrupt")
        self.assertEqual(
            CaptureLogReader(self.path).query("SELECT reason FROM segments"),
            [("aborted",)],
        )

    def test_invalid_annotations_and_documents_do_not_consume_events(self) -> None:
        for invalid in ({"nested": {}}, {"list": []}, {"nan": float("nan")}, {1: True}):
            with self.subTest(invalid=invalid), self.assertRaises(CaptureDecodeError):
                validate_attributes(invalid)
        with CaptureLogWriter(self.path, "capture") as writer:
            with self.assertRaises(CaptureDecodeError):
                writer.append_packet({"bad": float("inf")})
            with self.assertRaises(CaptureDecodeError):
                writer.append_marker("bad", "note", cast(Attributes, {"nested": {}}))
            self.assertEqual(writer.append_marker("good", "note"), 1)

    def test_abort_retains_only_committed_events_and_samples(self) -> None:
        writer = CaptureLogWriter(self.path, "capture")
        writer.register_device("a")
        writer.register_stream("a", self.spec())
        writer.append_packet_data(
            "a", "emg", [1], {'channel"one': [1.25]}, {'channel"one': [True]}, {}
        )
        writer.flush()
        writer.append_packet_data(
            "a", "emg", [2], {'channel"one': [2.5]}, {'channel"one': [True]}, {}
        )
        writer.abort()
        reader = CaptureLogReader(self.path)
        self.assertEqual(reader.query("SELECT count(*) FROM packets"), [(1,)])
        self.assertEqual(reader.query("SELECT count(*) FROM samples_0000"), [(1,)])
        self.assertEqual(
            reader.query("SELECT outcome FROM capture_status"), [("incomplete",)]
        )

    def test_reader_is_read_only_and_rejects_unsupported_schema(self) -> None:
        with CaptureLogWriter(self.path, "capture"):
            pass
        digest = hashlib.sha256(self.path.read_bytes()).digest()
        reader = CaptureLogReader(self.path)
        list(reader)
        with self.assertRaises(ValueError):
            reader.query("DELETE FROM events")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), digest)
        with duckdb.connect(str(self.path)) as connection:
            connection.execute("UPDATE capture SET schema_version=999")
        with self.assertRaises(CaptureDecodeError):
            list(reader)

    def test_reader_rejects_unknown_types_and_broken_sequences(self) -> None:
        with CaptureLogWriter(self.path, "capture"):
            pass
        with duckdb.connect(str(self.path)) as connection:
            connection.execute(
                "UPDATE events SET record_type='unknown' WHERE event_sequence=1"
            )
        with self.assertRaises(CaptureDecodeError):
            list(CaptureLogReader(self.path))

    def test_reader_rejects_corrupt_producer_sequence(self) -> None:
        with CaptureLogWriter(self.path, "capture"):
            pass
        with duckdb.connect(str(self.path)) as connection:
            connection.execute(
                "UPDATE events SET producer_sequence=5 WHERE event_sequence=1"
            )
        with self.assertRaisesRegex(CaptureDecodeError, "producer sequence"):
            list(CaptureLogReader(self.path))

    def test_existing_capture_is_not_overwritten(self) -> None:
        with CaptureLogWriter(self.path, "capture"):
            pass
        digest = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            CaptureLogWriter(self.path, "replacement")
        self.assertEqual(self.path.read_bytes(), digest)

    def test_export_rejects_table_name_filename_escape(self) -> None:
        with CaptureLogWriter(self.path, "capture"):
            pass
        with duckdb.connect(str(self.path)) as connection:
            connection.execute('CREATE TABLE "../escaped" (value INTEGER)')
        destination = self.path.with_name("export")
        with self.assertRaisesRegex(ValueError, "filename"):
            CaptureLogReader(self.path).export(destination, tables=["../escaped"])
        self.assertFalse(destination.exists())

    def test_recovery_of_missing_source_does_not_create_output(self) -> None:
        destination = self.path.with_name("recovered.capture.duckdb")
        with self.assertRaises(FileNotFoundError):
            recover_capture(self.path, destination)
        self.assertFalse(destination.exists())

    def test_process_death_recovery_copies_committed_prefix_without_mutation(
        self,
    ) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_crash_writer, args=(str(self.path), child))
        process.start()
        child.close()
        try:
            self.assertTrue(
                parent.poll(30), "writer did not reach its uncommitted batch"
            )
            parent.recv()
            process.terminate()
            process.join(10)
            self.assertFalse(process.is_alive())
            digest = hashlib.sha256(self.path.read_bytes()).digest()
            wal = self.path.with_name(self.path.name + ".wal")
            wal_digest = (
                hashlib.sha256(wal.read_bytes()).digest() if wal.exists() else None
            )
            recovered = self.path.with_name("recovered.capture.duckdb")
            recover_capture(self.path, recovered)
            records = list(CaptureLogReader(recovered))
            self.assertEqual(
                [r.marker_id for r in records if isinstance(r, Marker)], ["committed"]
            )
            self.assertFalse(any(isinstance(r, CaptureStopped) for r in records))
            self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), digest)
            if wal_digest is not None:
                self.assertEqual(hashlib.sha256(wal.read_bytes()).digest(), wal_digest)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            process.close()
            parent.close()


if __name__ == "__main__":
    unittest.main()
