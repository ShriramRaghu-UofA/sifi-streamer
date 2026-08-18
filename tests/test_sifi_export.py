import tempfile
import unittest
from compression import zstd
from pathlib import Path

import pandas as pd

from sifi_streamer.capture import (
    CaptureLogWriter,
    CaptureStarted,
    RawPacket,
    SegmentStarted,
    encode_record,
)
from sifi_streamer.sifi import DEFAULT_MODALITIES, Modality
from sifi_streamer.sifi.export import (
    SiFiExportError,
    export_sifi_capture_to_parquet,
    read_sifi_capture_tables,
)


def emg_packet(
    values: tuple[float | None, ...] = (1.0, 2.0),
    *,
    sample_rate: float = 1600.0,
) -> dict[str, object]:
    return {
        "packet_type": "emg_armband",
        "timestamps": [index / sample_rate for index in range(len(values))],
        "data": {f"emg{channel}": list(values) for channel in range(8)},
        "received_at": 10.0,
        "sample_rate": sample_rate,
        "samples_lost": 0,
        "status": "ok",
        "vendor_extension": {"preserved_in_capture": True},
    }


class SiFiExportTests(unittest.TestCase):
    def test_every_sifi_modality_uses_its_canonical_channel_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all.capture.jsonl.zst"
            with CaptureLogWriter(path, "all") as writer:
                for modality in Modality:
                    spec = DEFAULT_MODALITIES.require(modality)
                    writer.append_packet(
                        {
                            "packet_type": modality.value,
                            "timestamps": [1.0],
                            "data": {
                                channel: [float(index)]
                                for index, channel in enumerate(spec.channels)
                            },
                            "sample_rate": spec.sample_rate,
                        }
                    )
            tables = read_sifi_capture_tables(path)

        self.assertEqual(set(tables.signals), set(Modality))
        for modality, frame in tables.signals.items():
            expected = list(DEFAULT_MODALITIES.require(modality).channels)
            self.assertEqual(frame.columns[-len(expected) :].tolist(), expected)

    def test_tables_expand_samples_and_pair_generic_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.capture.jsonl.zst"
            with CaptureLogWriter(path, "session", {"operator": "one"}) as writer:
                writer.append_packet(
                    {
                        "info": {
                            "device": {
                                "emg": {"enabled": True, "fs": 1600},
                                "ecg": {"enabled": False, "fs": 500},
                            }
                        }
                    }
                )
                writer.start_segment(
                    "phase-1", "arbitrary", {"level": 2, "label": "rest"}
                )
                writer.append_marker(
                    "prompt-1",
                    "anything",
                    {"ready": True},
                    source_time_ns=123,
                    source_clock="display",
                )
                packet_sequence = writer.append_packet(emg_packet())
                writer.stop_segment("phase-1", "completed")

            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.capture.loc[0, "capture_id"], "session")
        self.assertEqual(tables.capture.loc[0, "attribute_operator"], "one")
        self.assertEqual(
            tables.streams["channel_id"].tolist(),
            [f"emg{channel}" for channel in range(8)],
        )
        self.assertEqual(
            tables.streams["rate_source"].unique().tolist(), ["device_info"]
        )
        signal = tables.signals[Modality.EMG]
        self.assertEqual(signal["packet_sequence"].tolist(), [packet_sequence] * 2)
        self.assertEqual(signal["sample_index_in_packet"].tolist(), [0, 1])
        self.assertEqual(signal["emg0"].tolist(), [1.0, 2.0])
        self.assertEqual(tables.markers.loc[0, "attribute_ready"], True)
        self.assertEqual(tables.markers.loc[0, "source_clock"], "display")
        self.assertEqual(str(tables.markers["source_time_ns"].dtype), "Int64")
        segment = tables.segments.iloc[0]
        self.assertLess(segment["start_sequence"], packet_sequence)
        self.assertGreater(segment["stop_sequence"], packet_sequence)
        self.assertEqual(segment["attribute_label"], "rest")

    def test_packet_rate_builds_manifest_without_device_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.capture.jsonl.zst"
            with CaptureLogWriter(path, "synthetic") as writer:
                writer.append_packet(emg_packet((1.0,), sample_rate=1000.0))
            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.streams["nominal_rate_hz"].unique().tolist(), [1000.0])
        self.assertEqual(tables.streams["rate_source"].unique().tolist(), ["packet"])

    def test_incompatible_attribute_types_and_normalized_names_fail(self) -> None:
        cases = (
            (("same", 1), ("same", "one")),
            (("A value", 1), ("a-value", 2)),
        )
        for first, second in cases:
            with (
                self.subTest(first=first, second=second),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "bad.capture.jsonl.zst"
                with CaptureLogWriter(path, "bad") as writer:
                    writer.append_marker("one", "kind", {first[0]: first[1]})
                    writer.append_marker("two", "kind", {second[0]: second[1]})
                    writer.append_packet(emg_packet((1.0,)))
                with self.assertRaises(SiFiExportError):
                    read_sifi_capture_tables(path)

    def test_misaligned_known_packet_fails_instead_of_dropping_samples(self) -> None:
        packet = emg_packet()
        data = packet["data"]
        assert isinstance(data, dict)
        data["emg7"] = [1.0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.capture.jsonl.zst"
            with CaptureLogWriter(path, "bad") as writer:
                writer.append_packet(packet)
            with self.assertRaisesRegex(SiFiExportError, "emg7"):
                read_sifi_capture_tables(path)

    def test_unknown_packets_are_ignored_but_sifi_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.capture.jsonl.zst"
            with CaptureLogWriter(path, "unknown") as writer:
                writer.append_packet({"packet_type": "future", "data": {}})
            with self.assertRaisesRegex(SiFiExportError, "neither SiFi"):
                read_sifi_capture_tables(path)

    def test_crash_truncated_capture_retains_open_segment_and_integer_clocks(
        self,
    ) -> None:
        host_time = 8_000_000_000_000_001
        records = (
            CaptureStarted(2, 0, host_time, host_time + 1, "crash", {}),
            SegmentStarted(2, 1, host_time + 2, host_time + 3, "phase", "kind", {}),
            RawPacket(
                2,
                2,
                host_time + 4,
                host_time + 5,
                emg_packet((1.0,)),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crash.capture.jsonl.zst"
            path.write_bytes(zstd.compress(b"".join(map(encode_record, records))))
            tables = read_sifi_capture_tables(path)

        self.assertEqual(
            tables.segments.loc[0, "start_host_monotonic_ns"], host_time + 2
        )
        self.assertTrue(pd.isna(tables.segments.loc[0, "stop_sequence"]))
        self.assertEqual(str(tables.segments["stop_sequence"].dtype), "Int64")
        self.assertTrue(pd.isna(tables.capture.loc[0, "stop_sequence"]))

    def test_reused_closed_segment_id_produces_distinct_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reused.capture.jsonl.zst"
            with CaptureLogWriter(path, "reused") as writer:
                for value in (1.0, 2.0):
                    writer.start_segment("phase", "kind")
                    writer.append_packet(emg_packet((value,)))
                    writer.stop_segment("phase", "completed")
            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.segments["segment_id"].tolist(), ["phase", "phase"])
        self.assertEqual(tables.signals[Modality.EMG]["emg0"].tolist(), [1.0, 2.0])

    def test_parquet_dataset_is_complete_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.capture.jsonl.zst"
            with CaptureLogWriter(path, "session") as writer:
                writer.append_packet(emg_packet((1.0,)))

            output = export_sifi_capture_to_parquet(path)
            self.assertEqual(output, root / "session.parquet")
            self.assertTrue((output / "capture.parquet").is_file())
            self.assertTrue((output / "streams.parquet").is_file())
            self.assertTrue((output / "markers.parquet").is_file())
            self.assertTrue((output / "segments.parquet").is_file())
            signal_path = output / "signals" / "emg_armband.parquet"
            self.assertEqual(pd.read_parquet(signal_path)["emg0"].tolist(), [1.0])
            with self.assertRaises(FileExistsError):
                export_sifi_capture_to_parquet(path)
            sentinel = output / "stale.txt"
            sentinel.write_text("old")
            self.assertEqual(export_sifi_capture_to_parquet(path, force=True), output)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
